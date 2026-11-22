import io
import os
import json
import threading
import urllib.error
import urllib.parse
import subprocess
import types
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path


import pytest

from life_logger.openclaw_bridge import OpenClawBridgeClient
from life_logger.local_web import (
    RESTART_INTERRUPTED_ERROR,
    JobStore,
    LocalWebServer,
    _ensure_mobile_access_token,
    _iso,
    _watch_direct_access_allowed,
    _synthesis_staleness,
    _watch_last_segment_received,
    create_server,
)
from scripts.local_jobs import run_web_job


def test_auto_ingest_waits_for_stable_files_and_dedupes_by_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    watch_dir = tmp_path / "recordings"
    watch_dir.mkdir()
    source = watch_dir / "2026-09-22_08-00-00.m4a"
    source.write_bytes(b"audio payload")
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    started: list[str] = []

    def fake_start(
        job_id: str,
        config_path: Path,
        *,
        secrets_path: Path | None = None,
    ) -> dict[str, object]:
        started.append(job_id)
        return store.read(job_id)

    monkeypatch.setattr(store, "start_job", fake_start)
    configured = store.configure_auto_ingest(
        enabled=True,
        folders=[str(watch_dir)],
        stability_seconds=5,
        scan_interval_seconds=5,
    )
    assert configured["enabled"] is True
    assert configured["folders"][0]["exists"] is True
    assert configured["history"][0]["kind"] == "configuration"

    first = store.scan_auto_ingest(config_path, now=100.0)
    assert first["pending_count"] == 1
    assert first["processed_count"] == 0
    assert first["scan_state"] == "waiting"
    assert first["last_discovered_count"] == 1
    assert started == []

    second = store.scan_auto_ingest(config_path, now=106.0)
    assert second["pending_count"] == 0
    assert second["processed_count"] == 1
    assert second["last_import_count"] == 1
    assert second["scan_state"] == "imported"
    assert second["history"][0]["kind"] == "import"
    assert second["history"][0]["files"] == [source.name]
    assert len(started) == 1
    jobs = store.list()
    assert len(jobs) == 1
    assert jobs[0]["files"][0]["original_name"] == source.name

    third = store.scan_auto_ingest(config_path, now=112.0)
    assert third["pending_count"] == 0
    assert third["processed_count"] == 1
    assert len(started) == 1
    assert len(store.list()) == 1


def test_h8_auto_ingest_folder_persists_and_creates_job_without_real_asr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-web"
    h8_record = tmp_path / "Volumes" / "H8精英版" / "RECORD"
    h8_record.mkdir(parents=True)
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")

    store = JobStore(root, recover=False)
    saved = store.add_auto_ingest_folder(str(h8_record))
    assert saved["enabled"] is False
    assert saved["folders"] == [
        {"path": str(h8_record.resolve()), "exists": True}
    ]

    restarted = JobStore(root, recover=False)
    persisted = restarted.auto_ingest_status()
    assert persisted["folders"][0]["path"] == str(h8_record.resolve())

    source = h8_record / "2026-09-22_12-00-00.wav"
    source.write_bytes(b"test audio only; ASR must not run")
    started: list[str] = []

    def fake_start(
        job_id: str,
        config_path: Path,
        *,
        secrets_path: Path | None = None,
    ) -> dict[str, object]:
        del config_path, secrets_path
        started.append(job_id)
        return restarted.read(job_id)

    monkeypatch.setattr(restarted, "start_job", fake_start)
    restarted.configure_auto_ingest(
        enabled=True,
        folders=[str(h8_record)],
        stability_seconds=5,
        scan_interval_seconds=5,
    )
    first = restarted.scan_auto_ingest(config_path, now=100.0)
    assert first["scan_state"] == "waiting"
    second = restarted.scan_auto_ingest(config_path, now=106.0)

    assert second["last_import_count"] == 1
    assert second["scan_state"] == "imported"
    assert len(started) == 1
    assert len(restarted.list()) == 1
    assert restarted.list()[0]["files"][0]["original_name"] == source.name


def test_auto_ingest_missing_folder_is_visible_in_status(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    missing = tmp_path / "Volumes" / "H8精英版" / "RECORD"
    store.configure_auto_ingest(enabled=True, folders=[str(missing)])

    status = store.scan_auto_ingest(tmp_path / "local-config.json", now=100.0)

    assert status["scan_state"] == "folder_missing"
    assert status["folder_errors"] == [
        {"path": str(missing.resolve()), "error": "目录未挂载或不存在"}
    ]


def test_auto_ingest_disabled_state_can_be_enabled_and_persisted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-web"
    watch_dir = tmp_path / "Volumes" / "H8精英版" / "RECORD"
    watch_dir.mkdir(parents=True)
    store = JobStore(root, recover=False)
    store.configure_auto_ingest(enabled=False, folders=[str(watch_dir)])

    enabled = store.configure_auto_ingest(
        enabled=True,
        folders=[str(watch_dir)],
        stability_seconds=20,
        scan_interval_seconds=15,
    )
    restarted = JobStore(root, recover=False)
    persisted = restarted.auto_ingest_status()

    assert enabled["enabled"] is True
    assert persisted["enabled"] is True
    assert persisted["folders"] == [
        {"path": str(watch_dir.resolve()), "exists": True}
    ]
    assert json.loads(restarted.auto_ingest_path().read_text(encoding="utf-8"))[
        "enabled"
    ] is True


def test_auto_ingest_rejects_relative_and_internal_paths(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)

    with pytest.raises(ValueError, match="绝对路径"):
        store.configure_auto_ingest(enabled=True, folders=["relative/folder"])

    with pytest.raises(ValueError, match="自己"):
        store.configure_auto_ingest(enabled=True, folders=[str(store.root)])


def test_auto_ingest_state_is_in_local_backup(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    watch_dir = tmp_path / "recordings"
    watch_dir.mkdir()
    store.configure_auto_ingest(enabled=False, folders=[str(watch_dir)])

    with zipfile.ZipFile(io.BytesIO(store.build_state_backup())) as archive:
        assert "auto-ingest.json" in archive.namelist()


def test_auto_ingest_native_folder_picker_returns_selected_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    selected = tmp_path / "recordings"
    selected.mkdir()

    class Result:
        returncode = 0
        stdout = f"{selected}\n"
        stderr = ""

    monkeypatch.setattr("life_logger.local_web.sys.platform", "darwin")
    monkeypatch.setattr(
        "life_logger.local_web.subprocess.run",
        lambda *args, **kwargs: Result(),
    )

    assert store.choose_auto_ingest_folder() == str(selected.resolve())


def test_auto_ingest_native_folder_picker_handles_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)

    class Result:
        returncode = 1
        stdout = ""
        stderr = "execution error: User canceled. (-128)"

    monkeypatch.setattr("life_logger.local_web.sys.platform", "darwin")
    monkeypatch.setattr(
        "life_logger.local_web.subprocess.run",
        lambda *args, **kwargs: Result(),
    )

    assert store.choose_auto_ingest_folder() is None

def test_iso_preserves_epoch_timestamp() -> None:
    assert datetime.fromisoformat(_iso(0)).timestamp() == 0


def test_mobile_access_token_persists_with_private_permissions(tmp_path: Path) -> None:
    root = tmp_path / "local-web"

    first = _ensure_mobile_access_token(root)
    second = _ensure_mobile_access_token(root)

    assert first == second
    assert len(first) >= 24
    secret_path = root / "secrets.json"
    payload = json.loads(secret_path.read_text(encoding="utf-8"))
    assert payload["mobile_access_token"] == first
    assert secret_path.stat().st_mode & 0o077 == 0


def test_non_local_bind_requires_explicit_lan_access(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    with pytest.raises(ValueError, match="局域网访问未启用"):
        create_server(host="0.0.0.0", port=0, store=store)

    server = create_server(
        host="0.0.0.0",
        port=0,
        store=store,
        lan_access_enabled=True,
    )
    try:
        assert server.server_address[0] == "0.0.0.0"
    finally:
        server.server_close()


def test_token_auth_cookie_csrf_and_lan_toggle(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    token = "test-mobile-access-token"
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        access_token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            urllib.request.urlopen(f"{base}/api/health")
        assert unauthorized.value.code == 401

        authenticated = urllib.request.Request(
            f"{base}/api/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        health = json.loads(urllib.request.urlopen(authenticated).read())
        assert health["ok"] is True
        assert "mobile_access_v1" in health["api_features"]

        settings_request = urllib.request.Request(
            f"{base}/api/settings",
            headers={"Authorization": f"Bearer {token}"},
        )
        settings_payload = json.loads(urllib.request.urlopen(settings_request).read())
        masked = settings_payload["mobile_access"]["token"]
        assert masked != token, "GET /api/settings 不得回显完整令牌"
        assert masked.startswith("test") and masked.endswith("en")
        assert "*" in masked
        assert settings_payload["mobile_access"]["token_masked"] == masked
        assert settings_payload["mobile_access"]["token_length"] == len(token)
        assert settings_payload["mobile_access"]["token_reveal_endpoint"] == "/api/settings/mobile-token"

        reveal_request = urllib.request.Request(
            f"{base}/api/settings/mobile-token",
            headers={"Authorization": f"Bearer {token}"},
        )
        revealed = json.loads(urllib.request.urlopen(reveal_request).read())
        assert revealed["token"] == token

        with pytest.raises(urllib.error.HTTPError) as reveal_unauthorized:
            urllib.request.urlopen(f"{base}/api/settings/mobile-token")
        assert reveal_unauthorized.value.code == 401

        empty_brief_request = urllib.request.Request(
            f"{base}/api/daily-brief",
            headers={"Authorization": f"Bearer {token}"},
        )
        empty_brief = json.loads(urllib.request.urlopen(empty_brief_request).read())
        assert empty_brief["schema_version"] == "agent-brief-v1"
        assert empty_brief["event_count"] == 0
        assert empty_brief["high_confidence_points"] == []
        assert empty_brief["warnings"] == ["今天尚无已处理记录。"]

        paired_url = f"{base}/?token={urllib.parse.quote(token)}"
        with urllib.request.urlopen(paired_url) as paired:
            set_cookie = paired.headers.get("Set-Cookie") or ""
            assert f"life_logger_access={token}" in set_cookie
            assert "HttpOnly" in set_cookie
            assert "SameSite=Strict" in set_cookie
        assert token not in "\n".join(server.access_log)

        missing_csrf = urllib.request.Request(
            f"{base}/api/mobile-access",
            data=b'{"enabled":true}',
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as forbidden:
            urllib.request.urlopen(missing_csrf)
        assert forbidden.value.code == 403

        enable = urllib.request.Request(
            f"{base}/api/mobile-access",
            data=b'{"enabled":true,"trusted_network_bind":"100.64.12.34"}',
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Origin": base,
                "X-Life-Logger-CSRF": "1",
            },
            method="POST",
        )
        enabled = json.loads(urllib.request.urlopen(enable).read())["mobile_access"]
        assert enabled["token"] != token and "*" in enabled["token"]
        assert enabled["token_masked"] == enabled["token"]
        assert enabled["lan_access_enabled"] is True
        assert enabled["trusted_network_bind"] == "100.64.12.34"
        assert enabled["desired_bind_host"] == "100.64.12.34"
        assert enabled["restart_required"] is True
        preferences = json.loads((store.root / "preferences.json").read_text())
        assert preferences["lan_access_enabled"] is True
        assert preferences["trusted_network_bind"] == "100.64.12.34"

        public_bind = urllib.request.Request(
            f"{base}/api/mobile-access",
            data=b'{"enabled":true,"trusted_network_bind":"8.8.8.8"}',
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Origin": base,
                "X-Life-Logger-CSRF": "1",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as invalid_bind:
            urllib.request.urlopen(public_bind)
        assert invalid_bind.value.code == 400
        persisted = json.loads((store.root / "preferences.json").read_text())
        assert persisted["trusted_network_bind"] == "100.64.12.34"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_watch_direct_access_requires_explicit_private_bind() -> None:
    assert _watch_direct_access_allowed("172.20.10.3", True) is True
    assert _watch_direct_access_allowed("100.64.20.3", True) is True
    assert _watch_direct_access_allowed("127.0.0.1", True) is False
    assert _watch_direct_access_allowed("0.0.0.0", True) is True
    assert _watch_direct_access_allowed("0.0.0.0", False) is False
    assert _watch_direct_access_allowed("172.20.10.3", False) is False
    assert _watch_direct_access_allowed("8.8.8.8", True) is False


def test_watch_health_stays_token_protected_on_loopback(tmp_path: Path) -> None:
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=JobStore(tmp_path / "local-web", recover=False),
        access_token="test-long-watch-token",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/watch-health"
            )
        assert unauthorized.value.code == 401
        assert server.watch_local_access_enabled is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_watch_direct_access_does_not_disable_auth_for_general_apis(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        access_token="test-long-watch-token",
    )
    server.watch_local_access_enabled = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        # [2026-10-14] watch-health now requires the embedded watch key to report
        # watch_upload_enabled=true (discovery hardening): a keyless probe still learns
        # "a Mac is here" but must NOT be selectable as the upload target.
        keyed_health = urllib.request.Request(
            f"{base}/api/watch-health",
            headers={"X-LifeLogger-Watch-Key": "lifelogger-watch-upload-key-v1"},
        )
        assert json.loads(urllib.request.urlopen(keyed_health).read())[
            "watch_upload_enabled"
        ] is True
        keyless_health = urllib.request.urlopen(f"{base}/api/watch-health")
        assert json.loads(keyless_health.read())["watch_upload_enabled"] is False
        # V67 探活不发密钥，Mac 用 HMAC 证明自己持有密钥。
        import hashlib
        import hmac
        challenged = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"{base}/api/watch-health",
            headers={"X-LifeLogger-Watch-Challenge": "abc"},
        )).read())
        assert challenged["watch_upload_enabled"] is True
        assert challenged["watch_challenge_response"] == hmac.new(
            b"lifelogger-watch-upload-key-v1", b"abc", hashlib.sha256
        ).hexdigest()

        create_session = urllib.request.Request(
            f"{base}/api/watch-sessions",
            data=json.dumps(
                {
                    "device_id": "watch-test",
                    "session_id": "session-test",
                    "started_at": "2026-09-25T10:00:00+08:00",
                    "sample_rate": 16_000,
                    "channels": 1,
                    "codec": "m4a-aac",
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                # Creating a session is no longer exempt on the strength of the source address:
                # the Watch route now requires the credential compiled into the Watch app.
                "X-LifeLogger-Watch-Key": "lifelogger-watch-upload-key-v1",
            },
            method="POST",
        )
        with urllib.request.urlopen(create_session) as response:
            assert response.status == 201

        # Without that credential the same Watch route is refused.
        keyless = urllib.request.Request(
            f"{base}/api/watch-sessions",
            data=json.dumps(
                {
                    "device_id": "watch-test-2",
                    "session_id": "session-test-2",
                    "started_at": "2026-09-25T10:00:00+08:00",
                    "sample_rate": 16_000,
                    "channels": 1,
                    "codec": "m4a-aac",
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as keyless_refused:
            urllib.request.urlopen(keyless)
        assert keyless_refused.value.code == 401

        preferences = urllib.request.Request(
            f"{base}/api/settings/preferences",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            urllib.request.urlopen(preferences)
        assert unauthorized.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_mobile_capture_config_persists_and_manifest_is_local(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    server = create_server(host="127.0.0.1", port=0, store=store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        capture_response = urllib.request.urlopen(f"{base}/api/mobile-capture")
        capture = json.loads(capture_response.read())
        assert capture["schema_version"] == "mobile-capture-v1"
        assert capture["enabled"] is True
        assert capture["auto_start"] is False
        assert capture["transport"] == "same-origin-job-upload"
        assert capture["requires_token"] is True
        assert capture["public_upload"] is False

        request = urllib.request.Request(
            f"{base}/api/mobile-capture",
            data=b'{"enabled":true,"auto_start":true}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        saved = json.loads(urllib.request.urlopen(request).read())["mobile_capture"]
        assert saved["enabled"] is True
        assert saved["auto_start"] is True

        preferences = json.loads((store.root / "preferences.json").read_text())
        assert preferences["mobile_capture_enabled"] is True
        assert preferences["mobile_capture_auto_start"] is True

        settings = json.loads(urllib.request.urlopen(f"{base}/api/settings").read())
        assert "mobile_capture_v1" in settings["api_features"]
        assert "mobile_shell_v1" not in settings["api_features"]
        assert settings["mobile_capture"]["auto_start"] is True
        assert "shell" not in settings["mobile_capture"]

        manifest = json.loads(
            urllib.request.urlopen(f"{base}/manifest.webmanifest").read()
        )
        assert manifest["display"] == "standalone"
        assert manifest["start_url"].startswith("/")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_authenticated_mobile_flow_reads_brief_and_confirms_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    source = store.job_path(job_id) / "files" / "abc" / "result" / "unknown-review.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(
            [{"segment_id": "seg-mobile", "text": "手机复核测试"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    relative = str(source.relative_to(store.job_path(job_id)))
    token = "mobile-flow-token"
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        access_token=token,
    )
    brief = {
        "schema_version": "daily-brief-v1",
        "date": "2026-09-22",
        "event_count": 1,
        "category_summary": {"todo": {"count": 1, "highlights": ["测试待办"]}},
        "todos": [{"title": "测试待办", "status": "pending"}],
        "warnings": [],
        "events": [
            {
                "event_id": "evt-mobile",
                "categories": ["todo"],
                "title": "测试待办",
                "confidence": 0.95,
            }
        ],
    }
    monkeypatch.setattr(server, "build_daily_brief", lambda **_kwargs: brief)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    auth_headers = {"Authorization": f"Bearer {token}"}
    try:
        brief_request = urllib.request.Request(
            f"{base}/api/daily-brief",
            headers=auth_headers,
        )
        received = json.loads(urllib.request.urlopen(brief_request).read())
        assert received["date"] == "2026-09-22"
        assert received["schema_version"] == "agent-brief-v1"
        assert received["event_count"] == 1
        assert received["todos"][0]["title"] == "测试待办"
        assert received["high_confidence_points"][0]["event_id"] == "evt-mobile"
        assert "events" not in received

        review = urllib.request.Request(
            f"{base}/api/jobs/{job_id}/review",
            data=json.dumps(
                {
                    "path": relative,
                    "segment_id": "seg-mobile",
                    "source_decision": "self",
                    "transcript_decision": "correct",
                }
            ).encode("utf-8"),
            headers={
                **auth_headers,
                "Content-Type": "application/json",
                "Origin": base,
                "X-Life-Logger-CSRF": "1",
            },
            method="POST",
        )
        saved = json.loads(urllib.request.urlopen(review).read())["review"]
        assert saved["segment_id"] == "seg-mobile"
        assert saved["source_decision"] == "self"
        saved_reviews = store.read_reviews(job_id, artifact_path=relative)
        assert saved_reviews[0]["segment_id"] == "seg-mobile"

        review_interlocutor = urllib.request.Request(
            f"{base}/api/jobs/{job_id}/review",
            data=json.dumps(
                {
                    "path": relative,
                    "segment_id": "seg-mobile",
                    "interlocutor_relation": "朋友",
                    "interlocutor_name": "小李",
                }
            ).encode("utf-8"),
            headers={
                **auth_headers,
                "Content-Type": "application/json",
                "Origin": base,
                "X-Life-Logger-CSRF": "1",
            },
            method="POST",
        )
        saved2 = json.loads(urllib.request.urlopen(review_interlocutor).read())["review"]
        assert saved2["interlocutor_relation"] == "friend"
        assert saved2["interlocutor_name"] == "小李"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_context_relation_review_overlay_is_authenticated_and_non_destructive(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    artifact = (
        store.job_path(job_id)
        / "files"
        / "abc"
        / "result"
        / "context-extraction.json"
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    candidate_payload = {
        "status": "completed",
        "relation_candidates": [
            {
                "candidate_id": "rel-123",
                "target_intent_id": "intent-456",
                "relation_type": "progress_of",
                "quote": "private transcript sentinel",
                "confidence": 0.91,
                "review_status": "pending",
            }
        ],
    }
    artifact.write_text(json.dumps(candidate_payload), encoding="utf-8")
    relative = str(artifact.relative_to(store.job_path(job_id)))
    intent_path = store.root / "active-intents.json"
    intent_path.write_text('{"intents":[{"intent_id":"intent-456"}]}', encoding="utf-8")
    artifact_before = artifact.read_bytes()
    intents_before = intent_path.read_bytes()

    token = "relation-review-token"
    server = create_server(
        host="127.0.0.1", port=0, store=store, access_token=token
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    auth_headers = {"Authorization": f"Bearer {token}"}

    def post_review(payload: dict[str, object]) -> tuple[int, str]:
        request = urllib.request.Request(
            f"{base}/api/jobs/{job_id}/context-relation-review",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                **auth_headers,
                "Content-Type": "application/json",
                "Origin": base,
                "X-Life-Logger-CSRF": "1",
            },
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")
        return response.status, response.read().decode("utf-8")

    try:
        status, body = post_review(
            {
                "path": relative,
                "candidate_id": "rel-123",
                "decision": "accepted",
                "note": "关联正确",
            }
        )
        assert status == 200
        response_text = body
        saved = json.loads(body)["review"]
        assert saved["candidate_id"] == "rel-123"
        assert saved["decision"] == "accepted"
        assert saved["note"] == "关联正确"
        assert "private transcript sentinel" not in response_text
        assert "target_intent_id" not in response_text

        for payload in (
            {"path": relative, "candidate_id": "missing", "decision": "accepted"},
            {"path": "../../active-intents.json", "candidate_id": "rel-123", "decision": "accepted"},
            {"path": relative, "candidate_id": "rel-123", "decision": "maybe"},
        ):
            invalid_status, _ = post_review(payload)
            assert invalid_status == 400

        status, body = post_review(
            {
                "path": relative,
                "candidate_id": "rel-123",
                "decision": "rejected",
            }
        )
        assert status == 200
        updated = json.loads(body)["review"]
        assert updated["decision"] == "rejected"
        assert len(updated["history"]) == 2

        request = urllib.request.Request(
            f"{base}/api/jobs/{job_id}/context-relation-reviews?path={urllib.parse.quote(relative)}",
            headers=auth_headers,
        )
        readback = json.loads(urllib.request.urlopen(request).read())
        assert len(readback["reviews"]) == 1
        assert readback["reviews"][0]["decision"] == "rejected"
        assert "private transcript sentinel" not in json.dumps(readback)
        assert artifact.read_bytes() == artifact_before
        assert intent_path.read_bytes() == intents_before
        assert store.context_relation_reviews_path(job_id).is_file()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_daily_brief_delivery_defaults_to_single_local_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    server = create_server(host="127.0.0.1", port=0, store=store)
    agent_brief = {
        "schema_version": "agent-brief-v1",
        "date": "2026-09-21",
        "timezone": "Asia/Shanghai",
        "event_count": 0,
        "category_summary": {},
        "todos": [],
        "warnings": [],
        "searchable_summary": "",
        "high_confidence_points": [],
    }
    monkeypatch.setattr(server, "build_agent_brief", lambda **_kwargs: agent_brief)

    payload = server.deliver_daily_brief(date="2026-09-21")

    outbox_path = Path(payload["delivery"]["outbox_path"])
    assert outbox_path.parent == (store.root / "outbox" / "daily-brief").resolve()
    assert json.loads(outbox_path.read_text(encoding="utf-8")) == agent_brief
    assert outbox_path.stat().st_mode & 0o777 == 0o600


def test_job_store_deduplicates_uploaded_content_and_keeps_time_provenance(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()

    record = store.add_file(
        job_id,
        filename="2026-09-09_08-30-00.m4a",
        relative_path="morning/2026-09-09_08-30-00.m4a",
        stream=io.BytesIO(b"same audio"),
        modified_at=0,
    )
    duplicate = store.add_file(
        job_id,
        filename="copy.m4a",
        relative_path="copy.m4a",
        stream=io.BytesIO(b"same audio"),
        modified_at=0,
    )

    status = store.read(job_id)
    assert duplicate["duplicate"] is True
    assert record["file"]["sha256"] == duplicate["file"]["sha256"]
    assert len(status["files"]) == 1
    assert status["files"][0]["time_source"] == "filename"
    assert status["files"][0]["time_precision"] == "exact"
    assert "time_calibration" not in status["files"][0]
    assert (store.job_path(job_id) / status["files"][0]["stored_path"]).exists()
    assert len(list((tmp_path / "local-web" / "content").iterdir())) == 1


def test_job_store_manual_start_overrides_filename_time(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    manual = "2026-09-22T08:00:00+08:00"
    job_id = store.create_job(start_at=manual)
    store.add_file(
        job_id,
        filename="2026-09-21_20-00-00.m4a",
        relative_path="2026-09-21_20-00-00.m4a",
        stream=io.BytesIO(b"audio"),
    )

    prepared = store.prepare_for_start(job_id)
    file_state = prepared["files"][0]

    assert file_state["effective_start_at"] == manual
    assert file_state["time_source"] == "user"
    assert file_state["time_precision"] == "exact"
    assert "time_calibration" not in file_state


def test_job_store_reuses_existing_job_by_exact_content_hash(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    original_id = store.create_job()
    store.add_file(
        original_id,
        filename="original.m4a",
        relative_path="original.m4a",
        stream=io.BytesIO(b"same exact audio"),
    )
    original_state = store.read(original_id)
    original_state["status"] = "interrupted"
    original_state["files"][0]["status"] = "interrupted"
    store.status_path(original_id).write_text(
        json.dumps(original_state),
        encoding="utf-8",
    )

    retry_id = store.create_job()
    reused = store.add_file(
        retry_id,
        filename="renamed-copy.m4a",
        relative_path="renamed-copy.m4a",
        stream=io.BytesIO(b"same exact audio"),
        reuse_existing=True,
    )

    assert reused["reused_existing_job"] is True
    assert reused["reusable_job_id"] == original_id
    assert reused["reusable_job_status"] == "interrupted"
    assert store.read(retry_id)["files"] == []


def test_exact_content_reuse_respects_processing_options(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    original_id = store.create_job(processing_options={"asr_model": "mlx-whisper-turbo"})
    store.add_file(
        original_id,
        filename="voice.m4a",
        relative_path="voice.m4a",
        stream=io.BytesIO(b"same exact audio"),
    )
    original_state = store.read(original_id)
    original_state["status"] = "interrupted"
    store.status_path(original_id).write_text(
        json.dumps(original_state),
        encoding="utf-8",
    )

    different_id = store.create_job(
        processing_options={"asr_model": "moss"}
    )
    uploaded = store.add_file(
        different_id,
        filename="voice.m4a",
        relative_path="voice.m4a",
        stream=io.BytesIO(b"same exact audio"),
        reuse_existing=True,
    )

    assert uploaded.get("reused_existing_job") is not True
    assert len(store.read(different_id)["files"]) == 1










def test_completed_empty_watch_transcript_finishes_without_attribution_or_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job(start_at="2026-10-06T10:00:00+08:00")
    store.add_file(
        job_id,
        filename="watch-session.m4a",
        relative_path="watch-session.m4a",
        stream=io.BytesIO(b"retained original audio"),
    )
    job_dir = store.job_path(job_id)
    calls: list[list[str]] = []
    file_hash = store.read(job_id)["files"][0]["sha256"]
    file_root = job_dir / "files" / file_hash
    asr_dir = file_root / "asr" / "2026-10-06"

    def fake_build_asr_command(**_kwargs):
        return ["run-empty-asr"]

    def fake_run(command, **_kwargs):
        calls.append(command)
        asr_dir.mkdir(parents=True, exist_ok=True)
        (asr_dir / "transcript.json").write_text("[]", encoding="utf-8")
        (asr_dir / "recordings.json").write_text("[]", encoding="utf-8")
        (asr_dir / "run-summary.json").write_text(
            json.dumps({"status": "completed", "transcript_count": 0}),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("scripts.local_jobs.build_asr_command", fake_build_asr_command)
    monkeypatch.setattr("scripts.local_jobs.run_command", fake_run)
    store.prepare_for_start(job_id)
    run_web_job(job_dir, {}, lambda event: store.update_from_worker(job_id, event))

    state = store.read(job_id)
    item = state["files"][0]
    assert calls == [["run-empty-asr"]]
    assert state["status"] == "completed"
    assert item["status"] == "completed"
    assert item["outcome"] == "no_speech"
    assert "原音频已保留" in item["outcome_reason"]
    assert (job_dir / item["stored_path"]).read_bytes() == b"retained original audio"
    assert any(path.endswith("/transcript.json") for path in item["results"])


@pytest.mark.parametrize("transcript_payload", ["{", "{}"])
def test_malformed_empty_transcript_remains_a_job_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transcript_payload: str,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job(start_at="2026-10-06T10:00:00+08:00")
    store.add_file(
        job_id,
        filename="watch-session.m4a",
        relative_path="watch-session.m4a",
        stream=io.BytesIO(b"retained original audio"),
    )
    job_dir = store.job_path(job_id)
    file_hash = store.read(job_id)["files"][0]["sha256"]
    asr_dir = job_dir / "files" / file_hash / "asr" / "2026-10-06"

    monkeypatch.setattr("scripts.local_jobs.build_asr_command", lambda **_kwargs: ["run-asr"])

    def fake_run(_command, **_kwargs):
        asr_dir.mkdir(parents=True, exist_ok=True)
        (asr_dir / "transcript.json").write_text(transcript_payload, encoding="utf-8")
        (asr_dir / "run-summary.json").write_text(
            json.dumps({"status": "completed", "transcript_count": 0}),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("scripts.local_jobs.run_command", fake_run)
    store.prepare_for_start(job_id)
    run_web_job(job_dir, {}, lambda event: store.update_from_worker(job_id, event))

    state = store.read(job_id)
    assert state["status"] == "failed"
    assert state["files"][0]["status"] == "failed"
    assert state["files"][0].get("outcome") is None




def test_store_marks_running_job_interrupted_after_server_restart(
    tmp_path: Path,
) -> None:
    first = JobStore(tmp_path / "local-web", recover=False)
    job_id = first.create_job()
    state_path = first.status_path(job_id)
    state = first.read(job_id)
    state.update(status="running", started_at=1_700_000_000.0)
    state["files"] = []
    state_path.write_text(json.dumps(state), encoding="utf-8")

    second = JobStore(tmp_path / "local-web", recover=True)

    recovered = second.read(job_id)
    assert recovered["status"] == "interrupted"
    assert recovered["started_at"] is None
    assert "服务器重启" in recovered["error"]


def test_store_rejects_incomplete_upload(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()

    with pytest.raises(ValueError, match="上传不完整"):
        store.add_file(
            job_id,
            filename="voice.wav",
            relative_path="voice.wav",
            stream=io.BytesIO(b"short"),
            max_bytes=10,
        )

    assert store.read(job_id)["files"] == []
    assert list((store.job_path(job_id) / "uploads").iterdir()) == []


def test_task_states_persist_separately_from_generated_events(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()

    saved = store.save_task_state(
        task_key=f"{job_id}:files/abc/result/2026-09-16/events.json:evt-1",
        status="in_progress",
        title="继续完善 Life Logger",
        event_id="evt-1",
        job_id=job_id,
        artifact_path="files/abc/result/2026-09-16/events.json",
    )

    assert saved["status"] == "in_progress"
    assert store.read_task_states()[0]["title"] == "继续完善 Life Logger"
    assert store.task_states_path().exists()

    updated = store.save_task_state(
        task_key=saved["task_key"],
        status="completed",
        job_id=job_id,
    )
    assert updated["status"] == "completed"
    assert updated["title"] == "继续完善 Life Logger"

    dismissed = store.save_task_state(
        task_key=saved["task_key"],
        status="dismissed",
        job_id=job_id,
    )
    assert dismissed["status"] == "dismissed"
    assert store.read_task_states()[0]["status"] == "dismissed"
    reloaded = JobStore(store.root, recover=False)
    assert reloaded.read_task_states()[0]["status"] == "dismissed"

    with pytest.raises(ValueError, match="任务状态"):
        store.save_task_state(task_key="x", status="deleted")


def test_local_http_api_uploads_file_and_preserves_status_after_refresh(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=tmp_path / "local-config.json",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        request = urllib.request.Request(
            f"{base}/api/jobs",
            data=b'{"timezone":"Asia/Shanghai"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        created = json.loads(urllib.request.urlopen(request).read())
        job_id = created["job_id"]

        upload = urllib.request.Request(
            f"{base}/api/jobs/{job_id}/files?name=voice.m4a",
            data=b"voice bytes",
            headers={
                "Content-Type": "audio/mp4",
                "Content-Length": "11",
                "X-Relative-Path": "folder/voice.m4a",
                "X-File-Mtime": "0",
            },
            method="PUT",
        )
        uploaded = json.loads(urllib.request.urlopen(upload).read())
        assert uploaded["file"]["original_name"] == "voice.m4a"

        refreshed = json.loads(
            urllib.request.urlopen(f"{base}/api/jobs/{job_id}").read()
        )
        assert refreshed["files"][0]["relative_path"] == "folder/voice.m4a"
        assert refreshed["status"] == "draft"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_local_http_api_streams_review_audio_with_range_requests(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    uploaded = store.add_file(
        job_id,
        filename="voice.wav",
        relative_path="voice.wav",
        stream=io.BytesIO(b"0123456789"),
    )
    file_hash = uploaded["file"]["sha256"]
    artifact = (
        store.job_path(job_id)
        / "files"
        / file_hash
        / "result"
        / "unknown-review.json"
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("[]", encoding="utf-8")
    relative = str(artifact.relative_to(store.job_path(job_id)))

    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=tmp_path / "local-config.json",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        request = urllib.request.Request(
            (
                f"{base}/api/jobs/{job_id}/audio?"
                f"path={urllib.parse.quote(relative)}"
            ),
            headers={"Range": "bytes=2-5"},
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 206
            assert response.headers["Accept-Ranges"] == "bytes"
            assert response.headers["Content-Range"] == "bytes 2-5/10"
            assert response.read() == b"2345"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_local_http_api_renders_precise_review_audio_clip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    uploaded = store.add_file(
        job_id,
        filename="voice.aac",
        relative_path="voice.aac",
        stream=io.BytesIO(b"fake aac"),
    )
    file_hash = uploaded["file"]["sha256"]
    artifact = (
        store.job_path(job_id)
        / "files"
        / file_hash
        / "result"
        / "unknown-review.json"
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("[]", encoding="utf-8")
    relative = str(artifact.relative_to(store.job_path(job_id)))
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        assert kwargs["check"] is True
        assert kwargs["capture_output"] is True
        return type("Result", (), {"stdout": b"RIFF-test", "stderr": b""})()

    # [2026-10-29 flaky 修复] 原来 patch 的是 "life_logger.local_web.subprocess.run"。
    # 注意 local_web 里是 `import subprocess`，所以 `local_web.subprocess` **就是全局 subprocess 模块**：
    # setattr 改的是 subprocess.run 这个**全局属性**，会污染同进程内所有线程。
    # 实测后果：上一个用例启动的 InputContextRuntimeManager 后台线程调
    #   subprocess.run(["lsappinfo","front"], check=...)
    # 会被本用例的 fake_run 接住 → commands 多出 1 条 → `assert len(commands) == 1` 失败；
    # 同时 fake_run 里 `kwargs["check"]` 对没传 check 的调用抛 KeyError，
    # 被 input_context_runtime.py:230 的 except 吞成 "Error in input context sample step: 'check'"。
    # 修法：把 **local_web 模块内的 subprocess 引用**换成局部替身（不动全局 subprocess 模块），
    # 这样别的模块（input_context_runtime / mac_input_context）仍然拿到真实 subprocess。
    fake_subprocess = types.SimpleNamespace(
        run=fake_run,
        CalledProcessError=subprocess.CalledProcessError,
        TimeoutExpired=subprocess.TimeoutExpired,
        SubprocessError=subprocess.SubprocessError,
    )
    monkeypatch.setattr("life_logger.local_web.subprocess", fake_subprocess)

    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=tmp_path / "local-config.json",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        url = (
            f"{base}/api/jobs/{job_id}/audio-clip?"
            f"path={urllib.parse.quote(relative)}&start=10&end=12"
        )
        with urllib.request.urlopen(url) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "audio/wav"
            assert response.read() == b"RIFF-test"
        assert len(commands) == 1
        command = commands[0]
        first_seek = command.index("-ss")
        second_seek = command.index("-ss", first_seek + 1)
        assert command[first_seek + 1] == "7.650"
        assert command[second_seek + 1] == "2.000"
        assert command[command.index("-t") + 1] == "2.700"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_local_http_api_reads_and_updates_task_state(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=tmp_path / "local-config.json",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        request = urllib.request.Request(
            f"{base}/api/tasks/status",
            data=json.dumps(
                {
                    "task_key": "task-1",
                    "status": "in_progress",
                    "title": "处理录音",
                    "job_id": job_id,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        updated = json.loads(urllib.request.urlopen(request).read())
        assert updated["task"]["status"] == "in_progress"

        payload = json.loads(urllib.request.urlopen(f"{base}/api/tasks").read())
        assert payload["tasks"][0]["task_key"] == "task-1"
        assert payload["tasks"][0]["title"] == "处理录音"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()





def test_health_and_state_backup_expose_new_backend_without_secrets(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    (store.root / "secrets.json").write_text(
        json.dumps({"typesafe_api_key": "do-not-export"}), encoding="utf-8"
    )
    (store.root / "personal-model.json").write_text(
        json.dumps({"version": "personal-test"}), encoding="utf-8"
    )
    store.event_corrections_path(job_id).write_text(
        json.dumps({"schema_version": "0.1", "corrections": {}}), encoding="utf-8"
    )
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=tmp_path / "local-config.json",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        health = json.loads(urllib.request.urlopen(f"{base}/api/health").read())
        assert health["ok"] is True
        assert "event_corrections_v1" in health["api_features"]
        assert "context_relation_review_v1" in health["api_features"]
        assert health["server_build_id"]
        assert health["server_started_at"]

        with urllib.request.urlopen(f"{base}/api/backup") as response:
            assert response.headers["Content-Type"] == "application/zip"
            archive_bytes = response.read()
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = set(archive.namelist())
            assert "manifest.json" in names
            assert "personal-model.json" in names
            assert f"jobs/{job_id}/status.json" in names
            assert f"jobs/{job_id}/event-corrections.json" in names
            assert "secrets.json" not in names
            assert all("content/" not in name for name in names)
            manifest = json.loads(archive.read("manifest.json"))
            assert "secrets.json" in manifest["excluded"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_data_health_reports_orphan_overlay(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    correction_path = store.event_corrections_path(job_id)
    correction_path.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "corrections": {
                    "missing::evt": {
                        "artifact_path": "files/missing/result/2026-09-16/events.json",
                        "event_id": "evt-missing",
                        "changes": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    report = store.data_health()

    assert report["status"] == "warning"
    assert report["issue_count"] >= 1
    assert any(item["kind"] == "missing_artifact" for item in report["issues"])
    assert report["notes"] == []


def test_data_health_reports_auto_ingest_missing_folder(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    missing = tmp_path / "missing-recordings"
    store.configure_auto_ingest(
        enabled=True,
        folders=[str(missing)],
        stability_seconds=5,
        scan_interval_seconds=5,
    )

    report = store.data_health()

    assert report["status"] == "warning"
    assert report["auto_ingest"]["configured"] is True
    assert report["auto_ingest"]["enabled"] is True
    assert report["auto_ingest"]["folder_count"] == 1
    assert report["auto_ingest"]["missing_folder_count"] == 1
    assert any(
        item["kind"] == "auto_ingest_missing_folder"
        and item["path"] == str(missing)
        for item in report["issues"]
    )

def test_local_model_preferences_are_validated_and_persisted(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    config_path = tmp_path / "local-config.json"
    config_path.write_text(
        json.dumps({"python": "python3", "asr": str(tmp_path / "asr"), "jev": {}}),
        encoding="utf-8",
    )
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=config_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        save = urllib.request.Request(
            f"{base}/api/settings/preferences",
            data=json.dumps(
                {
                    "onboarding_completed": True,
                    "asr_model": "mlx-whisper-turbo",
                    "speaker_model": "cam++",
                    "use_jev": False,
                    "decision_provider": "kev",
                    "jev_min_confidence": 0.72,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        saved = json.loads(urllib.request.urlopen(save).read())
        assert saved["preferences"]["onboarding_completed"] is True
        assert "decision_provider" not in saved["preferences"]
        assert (
            json.loads((store.root / "preferences.json").read_text())["asr_model"]
            == "mlx-whisper-turbo"
        )

        invalid = urllib.request.Request(
            f"{base}/api/settings/preferences",
            data=b'{"asr_model":"sensevoice-small"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(invalid)
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_review_decisions_persist_without_mutating_source_artifact(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    source = store.job_path(job_id) / "files" / "abc" / "result" / "unknown-review.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps([{"segment_id": "seg-1", "text": "测试内容"}], ensure_ascii=False),
        encoding="utf-8",
    )
    relative = str(source.relative_to(store.job_path(job_id)))

    saved = store.save_review_decision(
        job_id,
        artifact_path=relative,
        segment_id="seg-1",
        source_decision="self",
        transcript_decision="correct",
    )

    assert saved["source_decision"] == "self"
    assert saved["transcript_decision"] == "correct"
    assert (
        store.read_reviews(job_id, artifact_path=relative)[0]["segment_id"] == "seg-1"
    )
    assert json.loads(source.read_text(encoding="utf-8"))[0]["text"] == "测试内容"


def test_save_review_decisions_batch(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    source = store.job_path(job_id) / "files" / "abc" / "result" / "unknown-review.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(
            [
                {"segment_id": "seg-1", "text": "第1句"},
                {"segment_id": "seg-2", "text": "第2句"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    relative = str(source.relative_to(store.job_path(job_id)))

    results = store.save_review_decisions_batch(
        job_id,
        artifact_path=relative,
        items=[
            {"segment_id": "seg-1", "source_decision": "other_person", "transcript_decision": "correct"},
            {"segment_id": "seg-2", "source_decision": "media", "transcript_decision": "correct"},
        ],
    )

    assert len(results) == 2
    reviews = store.read_reviews(job_id, artifact_path=relative)
    reviews_by_id = {r["segment_id"]: r for r in reviews}
    assert reviews_by_id["seg-1"]["source_decision"] == "other_person"
    assert reviews_by_id["seg-2"]["source_decision"] == "media"


def test_review_decision_interlocutor_fields(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    source = store.job_path(job_id) / "files" / "abc" / "result" / "unknown-review.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(
            [
                {"segment_id": "seg-1", "text": "第1句"},
                {"segment_id": "seg-2", "text": "第2句"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    relative = str(source.relative_to(store.job_path(job_id)))

    # 1. Single save with Chinese relation normalized to English and interlocutor name
    saved = store.save_review_decision(
        job_id,
        artifact_path=relative,
        segment_id="seg-1",
        source_decision="other_person",
        interlocutor_relation="朋友",
        interlocutor_name="小李",
    )
    assert saved["source_decision"] == "other_person"
    assert saved["interlocutor_relation"] == "friend"
    assert saved["interlocutor_name"] == "小李"

    # 2. Update interlocutor only without changing source_decision
    updated = store.save_review_decision(
        job_id,
        artifact_path=relative,
        segment_id="seg-1",
        interlocutor_name="老李",
    )
    assert updated["source_decision"] == "other_person"
    assert updated["interlocutor_relation"] == "friend"
    assert updated["interlocutor_name"] == "老李"

    # 3. Batch save with interlocutors
    batch_res = store.save_review_decisions_batch(
        job_id,
        artifact_path=relative,
        items=[
            {
                "segment_id": "seg-2",
                "source_decision": "other_person",
                "interlocutor_relation": "colleague",
                "interlocutor_name": "张经理",
            }
        ],
    )
    assert len(batch_res) == 1
    assert batch_res[0]["interlocutor_relation"] == "colleague"
    assert batch_res[0]["interlocutor_name"] == "张经理"

    # 4. Read back all reviews
    reviews = store.read_reviews(job_id, artifact_path=relative)
    reviews_by_id = {r["segment_id"]: r for r in reviews}
    assert reviews_by_id["seg-1"]["interlocutor_name"] == "老李"
    assert reviews_by_id["seg-2"]["interlocutor_name"] == "张经理"
    assert reviews_by_id["seg-2"]["interlocutor_relation"] == "colleague"


def test_reviewed_self_segment_reenters_event_extraction(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    result_root = store.job_path(job_id) / "files" / "abc" / "result"
    source = result_root / "unknown-review.json"
    events_path = result_root / "2026-09-16" / "events.json"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(
            [
                {
                    "segment_id": "seg-cigarette",
                    "recording_id": "rec-1",
                    "start_offset_seconds": 120.0,
                    "end_offset_seconds": 123.0,
                    "absolute_started_at": "2026-09-16T18:11:23+08:00",
                    "absolute_ended_at": "2026-09-16T18:11:26+08:00",
                    "text": "花16元买了一包烟",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    events_path.write_text("[]", encoding="utf-8")
    source_relative = str(source.relative_to(store.job_path(job_id)))
    events_relative = str(events_path.relative_to(store.job_path(job_id)))

    store.save_review_decision(
        job_id,
        artifact_path=source_relative,
        segment_id="seg-cigarette",
        source_decision="self",
        transcript_decision="correct",
    )

    derived = store.read_manual_events(job_id, artifact_path=events_relative)
    assert len(derived) == 1
    assert derived[0]["title"] == "花16元买了一包烟"
    assert derived[0]["categories"] == ["expense"]
    assert derived[0]["data"]["expense"]["item"] == "香烟"
    assert derived[0]["review_status"] == "confirmed"
    assert json.loads(events_path.read_text(encoding="utf-8")) == []


def test_reviewed_watch_purchase_creates_confirmed_268_cny_expense(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    result_root = store.job_path(job_id) / "files" / "watch" / "result"
    source = result_root / "unknown-review.json"
    events_path = result_root / "2026-09-26" / "events.json"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(
            [
                {
                    "segment_id": "seg-watch-purchase",
                    "recording_id": "rec-watch-purchase",
                    "start_offset_seconds": 22.55,
                    "end_offset_seconds": 28.19,
                    "absolute_started_at": "2026-09-26T00:09:44.550000+08:00",
                    "absolute_ended_at": "2026-09-26T00:09:50.190000+08:00",
                    "text": "买了一个智慧充电口，268，然后去超市准备消费。",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    events_path.write_text("[]", encoding="utf-8")
    source_relative = str(source.relative_to(store.job_path(job_id)))
    events_relative = str(events_path.relative_to(store.job_path(job_id)))

    store.save_review_decision(
        job_id,
        artifact_path=source_relative,
        segment_id="seg-watch-purchase",
        source_decision="self",
        transcript_decision="correct",
    )

    derived = store.read_manual_events(job_id, artifact_path=events_relative)
    assert len(derived) == 1
    assert derived[0]["categories"] == ["expense"]
    assert derived[0]["data"]["expense"]["amount"] == 268.0
    assert derived[0]["data"]["expense"]["item"] == "智慧充电口"
    assert derived[0]["review_status"] == "confirmed"
    assert json.loads(events_path.read_text(encoding="utf-8")) == []


def test_reviewed_purchase_does_not_leak_into_distant_neighbor_segment(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    result_root = store.job_path(job_id) / "files" / "watch" / "result"
    source = result_root / "unknown-review.json"
    events_path = result_root / "2026-09-26" / "events.json"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "segment_id": "purchase",
            "recording_id": "watch-session",
            "start_offset_seconds": 22.55,
            "end_offset_seconds": 28.19,
            "absolute_started_at": "2026-09-26T00:09:44.550000+08:00",
            "absolute_ended_at": "2026-09-26T00:09:50.190000+08:00",
            "text": "买了一个智慧充电口，268。",
        },
        {
            "segment_id": "unrelated",
            "recording_id": "watch-session",
            "start_offset_seconds": 40.93,
            "end_offset_seconds": 49.27,
            "absolute_started_at": "2026-09-26T00:10:02.930000+08:00",
            "absolute_ended_at": "2026-09-26T00:10:11.270000+08:00",
            "text": "出门忘带钥匙了，必须找房东拿备用钥匙。",
        },
    ]
    source.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    events_path.write_text("[]", encoding="utf-8")
    source_relative = str(source.relative_to(store.job_path(job_id)))
    events_relative = str(events_path.relative_to(store.job_path(job_id)))
    for record in records:
        store.save_review_decision(
            job_id,
            artifact_path=source_relative,
            segment_id=record["segment_id"],
            source_decision="self",
            transcript_decision="correct",
        )

    derived = store.read_manual_events(job_id, artifact_path=events_relative)
    expenses = [event for event in derived if "expense" in event.get("categories", [])]
    assert len(expenses) == 1
    assert expenses[0]["data"]["expense"]["amount"] == 268.0
    assert expenses[0]["data"]["expense"]["item"] == "智慧充电口"


def test_manual_events_are_stored_separately_from_generated_events(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    source = (
        store.job_path(job_id)
        / "files"
        / "abc"
        / "result"
        / "2026-09-16"
        / "events.json"
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("[]", encoding="utf-8")
    relative = str(source.relative_to(store.job_path(job_id)))

    saved = store.save_manual_event(
        job_id,
        artifact_path=relative,
        title="购买香烟",
        categories=["expense"],
        absolute_time="2026-09-16T18:11:23+08:00",
        time_precision="inferred",
        activity_type="purchase",
        data={"expense": {"amount": None, "currency": "CNY", "item": "香烟"}},
        note="用户确认发生过；时间按离开节点推定",
    )

    assert saved["source_type"] == "manual"
    assert saved["review_status"] == "confirmed"
    assert (
        store.read_manual_events(job_id, artifact_path=relative)[0]["title"]
        == "购买香烟"
    )
    assert json.loads(source.read_text(encoding="utf-8")) == []


def test_global_search_uses_full_asr_transcript_and_result_events_only(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    result_root = store.job_path(job_id) / "files" / "abc" / "result"
    day_root = result_root / "2026-09-16"
    day_root.mkdir(parents=True, exist_ok=True)
    (result_root / "self-transcript.json").write_text(
        json.dumps(
            [
                {
                    "segment_id": "self-1",
                    "recording_id": "rec-1",
                    "absolute_started_at": "2026-09-16T18:11:23+08:00",
                    "text": "我刚从楼下回来，顺手买了包烟。",
                    "review_required": False,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (result_root / "non-self-transcript.json").write_text(
        json.dumps(
            [
                {
                    "segment_id": "media-1",
                    "absolute_started_at": "2026-09-16T18:12:00+08:00",
                    "text": "直播密码是 7788",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (day_root / "events.json").write_text(
        json.dumps(
            [
                {
                    "event_id": "evt-1",
                    "categories": ["expense"],
                    "title": "购买香烟",
                    "time": {"absolute": "2026-09-16T18:11:23+08:00"},
                    "source_type": "audio",
                    "review_status": "unreviewed",
                    "evidence": {"quote": "我刚从楼下回来，顺手买了包烟。"},
                },
                {
                    "event_id": "evt-dup",
                    "categories": ["expense"],
                    "title": "然后购买香烟",
                    "time": {"absolute": "2026-09-16T18:12:03+08:00"},
                    "source_type": "audio",
                    "review_status": "unreviewed",
                    "evidence": {"quote": "我刚从楼下回来，顺手买了包烟。"},
                },
                {
                    "event_id": "evt-noise",
                    "categories": ["work_communication"],
                    "title": "我这两年一直把这个项目给忘记了",
                    "time": {"absolute": "2026-09-16T18:15:00+08:00"},
                    "source_type": "audio",
                    "review_status": "unreviewed",
                    "evidence": {"quote": "这个项目一直没录音，录音设备。"},
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    asr_root = store.job_path(job_id) / "files" / "abc" / "asr" / "2026-09-16"
    asr_root.mkdir(parents=True, exist_ok=True)
    (asr_root / "transcript.json").write_text(
        json.dumps(
            [
                {
                    "segment_id": "full-1",
                    "recording_id": "rec-1",
                    "absolute_started_at": "2026-09-16T18:11:23+08:00",
                    "text": "我刚从楼下回来，顺手买了包烟。",
                    "source": "local_funasr_nano",
                },
                {
                    "segment_id": "full-2",
                    "recording_id": "rec-1",
                    "absolute_started_at": "2026-09-16T18:12:00+08:00",
                    "text": "直播密码是 7788",
                    "source": "local_funasr_nano",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (asr_root / "events.json").write_text(
        json.dumps(
            [{"event_id": "noise", "title": "直播密码 7788"}], ensure_ascii=False
        ),
        encoding="utf-8",
    )

    hits = store.search("买", date="2026-09-16")
    assert {item["result_type"] for item in hits} == {"utterance", "event"}
    utterance = next(item for item in hits if item["result_type"] == "utterance")
    assert utterance["artifact_path"].startswith("files/abc/asr/")
    assert utterance["source_label"] == "full_transcript"
    media_hits = store.search("直播密码")
    assert len(media_hits) == 1
    assert media_hits[0]["result_type"] == "utterance"
    assert media_hits[0]["source_label"] == "full_transcript"
    assert store.search("直播密码", result_type="event") == []
    assert store.search("这个项目一直没录音", result_type="event") == []
    assert [
        item["result_type"] for item in store.search("购买香烟", result_type="event")
    ] == ["event"]
    filtered = store.search(
        "",
        result_type="all",
        category="expense",
        date_from="2026-09-15",
        date_to="2026-09-16",
    )
    assert len(filtered) == 1
    assert filtered[0]["result_type"] == "event"
    assert filtered[0]["categories"] == ["expense"]
    assert store.search(
        "",
        result_type="event",
        category="expense",
        date_from="2026-09-17",
    ) == []
    assert store.search("", result_type="all") == []
    with pytest.raises(ValueError, match="开始日期不能晚于结束日期"):
        store.search("", date_from="2026-09-18", date_to="2026-09-16")


def test_search_filters_and_labels_import_source_for_utterances_and_events(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    job_dir = store.job_path(job_id)
    state = store._read_unlocked(job_id)
    state["files"] = [
        {"file_id": "watch", "capture_source": "apple-watch"},
        {"file_id": "passport", "capture_source": "ai-passport-wifi"},
        {"file_id": "upload"},
    ]
    store._save_unlocked(state)
    for file_id, title in (
        ("watch", "Watch event"),
        ("passport", "Passport event"),
        ("upload", "Uploaded event"),
    ):
        result_dir = job_dir / "files" / file_id / "result" / "2026-09-16"
        result_dir.mkdir(parents=True)
        (result_dir / "events.json").write_text(
            json.dumps(
                [{
                    "event_id": f"event-{file_id}",
                    "categories": ["routine"],
                    "title": title,
                    "time": {"absolute": "2026-09-16T18:11:23+08:00"},
                    "evidence": {"quote": title},
                }],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        asr_dir = job_dir / "files" / file_id / "asr" / "2026-09-16"
        asr_dir.mkdir(parents=True)
        (asr_dir / "transcript.json").write_text(
            json.dumps([{
                "segment_id": f"utterance-{file_id}",
                "absolute_started_at": "2026-09-16T18:11:23+08:00",
                "text": title,
            }], ensure_ascii=False),
            encoding="utf-8",
        )

    all_hits = store.search("event", import_source="all")
    assert {item["import_source"] for item in all_hits} == {
        "apple-watch", "ai-passport-wifi", "file-upload"
    }
    watch_hits = store.search("", import_source="apple-watch")
    assert {item["result_type"] for item in watch_hits} == {"utterance", "event"}
    assert {item["import_source"] for item in watch_hits} == {"apple-watch"}
    assert {item["text"] for item in store.search("", import_source="file-upload")} == {
        "Uploaded event"
    }


def test_search_category_aliases_match_canonical_categories(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    events_path = (
        store.job_path(job_id)
        / "files"
        / "abc"
        / "result"
        / "2026-09-16"
        / "events.json"
    )
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps(
            [
                {
                    "event_id": "evt-food",
                    "categories": ["food"],
                    "title": "早餐吃了鸡蛋",
                    "time": {"absolute": "2026-09-16T08:00:00+08:00"},
                    "source_type": "audio",
                    "review_status": "unreviewed",
                    "evidence": {"quote": "早餐吃了两个鸡蛋。"},
                },
                {
                    "event_id": "evt-work",
                    "categories": ["work_communication"],
                    "title": "和同事沟通项目方案",
                    "time": {"absolute": "2026-09-16T10:00:00+08:00"},
                    "source_type": "audio",
                    "review_status": "unreviewed",
                    "evidence": {"quote": "上午和同事沟通了项目方案。"},
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert [
        item["event_id"]
        for item in store.search("", result_type="event", category="diet")
    ] == ["evt-food"]
    for alias in ("work", "communication"):
        assert [
            item["event_id"]
            for item in store.search("", result_type="event", category=alias)
        ] == ["evt-work"]


def test_search_filters_expense_amount_range(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    events_path = (
        store.job_path(job_id)
        / "files"
        / "abc"
        / "result"
        / "2026-09-16"
        / "events.json"
    )
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps(
            [
                {
                    "event_id": "evt-8",
                    "categories": ["expense"],
                    "title": "早餐 8 元",
                    "time": {"absolute": "2026-09-16T08:00:00+08:00"},
                    "data": {"expense": {"amount": 8, "currency": "CNY"}},
                    "source_type": "audio",
                    "review_status": "unreviewed",
                    "evidence": {"quote": "早餐花了8元。"},
                },
                {
                    "event_id": "evt-16",
                    "categories": ["expense"],
                    "title": "短剧 16 元",
                    "time": {"absolute": "2026-09-16T09:00:00+08:00"},
                    "data": {"expense": {"amount": 16, "currency": "CNY"}},
                    "source_type": "audio",
                    "review_status": "unreviewed",
                    "evidence": {"quote": "花16买了一包短剧。"},
                },
                {
                    "event_id": "evt-null",
                    "categories": ["expense"],
                    "title": "金额未知的消费",
                    "time": {"absolute": "2026-09-16T10:00:00+08:00"},
                    "data": {"expense": {"amount": None, "currency": "CNY"}},
                    "source_type": "audio",
                    "review_status": "unreviewed",
                    "evidence": {"quote": "买了东西。"},
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    hits = store.search(
        "",
        result_type="all",
        category="expense",
        date_from="2026-09-16",
        date_to="2026-09-16",
        amount_min=10,
        amount_max=20,
    )

    assert [item["event_id"] for item in hits] == ["evt-16"]
    assert hits[0]["expense_amount_cny"] == 16.0
    assert store.search("", result_type="all", amount_min=15)[0]["event_id"] == (
        "evt-16"
    )
    with pytest.raises(ValueError, match="最低金额不能高于最高金额"):
        store.search("", amount_min=20, amount_max=10)
    with pytest.raises(ValueError, match="不能小于 0"):
        store.search("", amount_min=-1)


def test_global_search_includes_manual_confirmed_events(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    source = (
        store.job_path(job_id)
        / "files"
        / "abc"
        / "result"
        / "2026-09-16"
        / "events.json"
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("[]", encoding="utf-8")
    relative = str(source.relative_to(store.job_path(job_id)))
    store.save_manual_event(
        job_id,
        artifact_path=relative,
        title="购买香烟",
        categories=["expense"],
        absolute_time="2026-09-16T18:11:23+08:00",
    )

    hits = store.search("香烟", result_type="event")
    assert len(hits) == 1
    assert hits[0]["source_type"] == "manual"
    assert hits[0]["review_status"] == "confirmed"



def test_event_corrections_overlay_generated_events_and_search(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    events_path = (
        store.job_path(job_id)
        / "files"
        / "abc"
        / "result"
        / "2026-09-16"
        / "events.json"
    )
    events_path.parent.mkdir(parents=True, exist_ok=True)
    original = [
        {
            "event_id": "evt-1",
            "categories": ["expense"],
            "title": "购买香烟",
            "time": {"absolute": "2026-09-16T18:11:23+08:00"},
            "data": {"expense": {"amount": 20, "currency": "CNY"}},
            "source_type": "audio",
            "review_status": "unreviewed",
            "evidence": {"quote": "我刚从楼下回来。"},
        }
    ]
    events_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    relative = str(events_path.relative_to(store.job_path(job_id)))

    correction = store.save_event_correction(
        job_id,
        artifact_path=relative,
        event_id="evt-1",
        changes={
            "title": "购买矿泉水",
            "categories": ["expense", "food"],
            "data": {
                "expense": {"amount": 3.5, "currency": "CNY"},
                "food": {"item": "矿泉水"},
            },
            "note": "人工核对后修正",
        },
        rejected=False,
    )

    assert correction["rejected"] is False
    hits = store.search("矿泉水", result_type="event")
    assert len(hits) == 1
    assert hits[0]["text"] == "购买矿泉水"
    assert hits[0]["categories"] == ["expense", "food"]
    assert store.search("购买香烟", result_type="event") == []
    saved = store.read_event_corrections(job_id, artifact_path=relative)
    assert len(saved) == 1
    assert saved[0]["changes"]["note"] == "人工核对后修正"

    store.save_event_correction(
        job_id,
        artifact_path=relative,
        event_id="evt-1",
        rejected=True,
    )
    assert store.search("矿泉水", result_type="event") == []
    assert json.loads(events_path.read_text(encoding="utf-8")) == original



def test_rejected_manual_event_can_be_read_for_restore(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    events_path = (
        store.job_path(job_id)
        / "files"
        / "abc"
        / "result"
        / "2026-09-16"
        / "events.json"
    )
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text("[]", encoding="utf-8")
    relative = str(events_path.relative_to(store.job_path(job_id)))
    manual = store.save_manual_event(
        job_id,
        artifact_path=relative,
        title="人工确认事件",
        categories=["idea"],
        absolute_time="2026-09-16T19:00:00+08:00",
    )

    store.save_event_correction(
        job_id,
        artifact_path=relative,
        event_id=manual["event_id"],
        rejected=True,
    )

    assert store.read_manual_events(job_id, artifact_path=relative) == []
    rejected = store.read_manual_events(
        job_id,
        artifact_path=relative,
        include_rejected=True,
    )
    assert len(rejected) == 1
    assert rejected[0]["review_status"] == "rejected"

    store.save_event_correction(
        job_id,
        artifact_path=relative,
        event_id=manual["event_id"],
        rejected=False,
    )
    restored = store.read_manual_events(job_id, artifact_path=relative)
    assert len(restored) == 1
    assert restored[0]["review_status"] == "confirmed"

def test_incorrect_transcript_can_store_corrected_text_and_rebuild_events(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    result_root = store.job_path(job_id) / "files" / "abc" / "result"
    source = result_root / "unknown-review.json"
    events_path = result_root / "2026-09-16" / "events.json"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(
            [
                {
                    "segment_id": "seg-corrected",
                    "recording_id": "rec-1",
                    "start_offset_seconds": 120.0,
                    "end_offset_seconds": 123.0,
                    "absolute_started_at": "2026-09-16T18:11:23+08:00",
                    "absolute_ended_at": "2026-09-16T18:11:26+08:00",
                    "text": "刚回来",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    events_path.write_text("[]", encoding="utf-8")
    source_relative = str(source.relative_to(store.job_path(job_id)))
    events_relative = str(events_path.relative_to(store.job_path(job_id)))

    saved = store.save_review_decision(
        job_id,
        artifact_path=source_relative,
        segment_id="seg-corrected",
        source_decision="self",
        transcript_decision="incorrect",
        corrected_text="花16元买了一包烟",
    )

    assert saved["corrected_text"] == "花16元买了一包烟"
    derived = store.read_manual_events(job_id, artifact_path=events_relative)
    assert len(derived) == 1
    assert derived[0]["title"] == "花16元买了一包烟"
    assert derived[0]["review_status"] == "confirmed"
    assert derived[0]["evidence"]["original_asr_text"] == "刚回来"
    assert derived[0]["evidence"]["corrected_text"] == "花16元买了一包烟"

def test_web_job_resume_skips_completed_stage_without_real_asr(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job(start_at="2026-09-09T08:00:00+08:00")
    store.add_file(
        job_id,
        filename="voice.wav",
        relative_path="voice.wav",
        stream=io.BytesIO(b"fake audio"),
    )
    job_dir = store.job_path(job_id)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "run_cloud_asr_pipeline.py" in command[1] or "run_full_funasr_pipeline.py" in command[1]:
            file_hash = store.read(job_id)["files"][0]["sha256"]
            asr_dir = job_dir / "files" / file_hash / "asr"
            day = asr_dir / "2026-09-09"
            day.mkdir(parents=True, exist_ok=True)
            (day / "transcript.json").write_text("[]", encoding="utf-8")
            (day / "recordings.json").write_text("[]", encoding="utf-8")
        else:
            file_hash = store.read(job_id)["files"][0]["sha256"]
            result = job_dir / "files" / file_hash / "result"
            result.mkdir(parents=True, exist_ok=True)
            (result / "daily.md").write_text("# test\n", encoding="utf-8")
            (result / "events.json").write_text("[]", encoding="utf-8")
        return 0

    monkeypatch.setattr("scripts.local_jobs.run_command", fake_run)
    store.prepare_for_start(job_id)
    config = {
        "python": "python",
        "asr": "fun",
        "vad": "vad",
        "template": "template",
        "speaker": "cam++",
    }

    def callback(event):
        store.update_from_worker(job_id, event)

    run_web_job(job_dir, config, callback)
    assert len(calls) == 2

    store.prepare_for_start(job_id)
    run_web_job(job_dir, config, callback)
    assert len(calls) == 2
    assert store.read(job_id)["status"] == "completed"


def test_restart_recovery_marks_active_job_and_remembers_it(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    store = JobStore(root, recover=False)
    job_id = store.create_job()
    store.add_file(
        job_id,
        filename="voice.wav",
        relative_path="voice.wav",
        stream=io.BytesIO(b"fake audio"),
    )
    state = store.read(job_id)
    state["status"] = "running"
    state["started_at"] = 1.0
    state["current_stage"] = "asr"
    state["files"][0]["status"] = "running"
    state["files"][0]["current_stage"] = "asr"
    state["files"][0]["stages"] = {
        "asr": {"status": "running", "started_at": 1.0}
    }
    store.status_path(job_id).write_text(
        json.dumps(state, ensure_ascii=False),
        encoding="utf-8",
    )

    recovered = JobStore(root, recover=True)
    saved = recovered.read(job_id)

    assert recovered.startup_recovery_ids == (job_id,)
    assert saved["status"] == "interrupted"
    assert saved["current_stage"] == "interrupted"
    assert saved["error"] == RESTART_INTERRUPTED_ERROR
    assert saved["restart_recovery_pending"] is True
    assert saved["files"][0]["status"] == "interrupted"
    assert saved["files"][0]["stages"]["asr"]["status"] == "interrupted"


def test_server_starts_background_recovery_for_restart_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-web"
    store = JobStore(root, recover=False)
    job_id = store.create_job()
    store.add_file(
        job_id,
        filename="voice.wav",
        relative_path="voice.wav",
        stream=io.BytesIO(b"fake audio"),
    )
    state = store.read(job_id)
    state["status"] = "queued"
    store.status_path(job_id).write_text(json.dumps(state), encoding="utf-8")
    recovered = JobStore(root, recover=True)
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    called = threading.Event()
    captured: list[list[str]] = []

    def fake_resume(
        config_path: Path,
        *,
        secrets_path: Path | None = None,
        job_ids=None,
    ) -> list[str]:
        del config_path, secrets_path
        captured.append(list(job_ids or []))
        called.set()
        return list(job_ids or [])

    monkeypatch.setattr(recovered, "resume_interrupted_jobs", fake_resume)
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=recovered,
        config_path=config_path,
    )
    try:
        assert called.wait(1.0)
        assert captured == [[job_id]]
    finally:
        server.server_close()


def test_recover_interrupted_api_schedules_legacy_interrupted_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    state = store.read(job_id)
    state["status"] = "interrupted"
    store.status_path(job_id).write_text(json.dumps(state), encoding="utf-8")
    server = create_server(host="127.0.0.1", port=0, store=store)
    monkeypatch.setattr(server, "recover_interrupted_jobs", lambda: [job_id])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/jobs/recover-interrupted",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        payload = json.loads(urllib.request.urlopen(request).read())
        assert payload == {"scheduled": [job_id], "count": 1}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_doctor_endpoint_exposes_local_self_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    expected = {
        "ok": True,
        "checks": [{"key": "service_health", "ok": True}],
        "job_stats": {"total": 0, "recent": []},
    }

    def fake_doctor(config_path_arg, **kwargs):
        assert Path(config_path_arg) == config_path
        assert kwargs["job_root"] == store.root
        assert kwargs["web_health"]["ok"] is True
        return expected

    monkeypatch.setattr("life_logger.local_web.run_doctor", fake_doctor)
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=config_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.loads(
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/doctor"
            ).read()
        )
        assert payload == expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_agent_daily_endpoint_returns_summary_and_event_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    server = create_server(host="127.0.0.1", port=0, store=store)
    expected_brief = {
        "schema_version": "daily-brief-v1",
        "date": "2026-09-21",
        "event_count": 1,
        "category_summary": {},
        "todos": [],
        "warnings": [],
        "events": [{"event_id": "evt-1", "title": "测试事件"}],
    }

    def fake_brief(*, date=None):
        assert date == "2026-09-21"
        return expected_brief

    monkeypatch.setattr(server, "build_daily_brief", fake_brief)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.loads(
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/daily?date=2026-09-21"
            ).read()
        )
        assert payload["date"] == "2026-09-21"
        assert payload["daily_summary"] == expected_brief
        assert payload["events"] == expected_brief["events"]
        assert "nutrition_ledger" in payload
        assert "fitness_ledger" in payload
        assert "smoking_ledger" in payload
        assert "expense_ledger" in payload
        assert "work_cognition_ledger" in payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_daily_and_brief_expose_stale_synthesis_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "local-web"
    synth_dir = root / "daily-synthesis"
    synth_dir.mkdir(parents=True)
    (synth_dir / "synthesis-2026-10-22.json").write_text(
        json.dumps(
            {
                "event_count": 17,
                "generated_at": "2026-10-23T02:38:37+08:00",
                "event_count_basis": "caller-supplied event_view events after read-layer filtering",
            }
        ),
        encoding="utf-8",
    )
    store = JobStore(root, recover=False)
    server = create_server(host="127.0.0.1", port=0, store=store)
    brief = {
        "schema_version": "daily-brief-v1",
        "date": "2026-10-22",
        "event_count": 22,
        "generated_at": "2026-10-24T14:24:41+08:00",
        "events": [],
        "category_summary": {},
        "todos": [],
        "warnings": [],
    }

    def fake_daily_brief(*, date=None, **_kwargs):
        assert date == "2026-10-22"
        return dict(brief)

    def fake_agent_brief(*, date=None, **_kwargs):
        assert date == "2026-10-22"
        return dict(brief)

    monkeypatch.setattr(server, "build_daily_brief", fake_daily_brief)
    monkeypatch.setattr(server, "build_agent_brief", fake_agent_brief)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        daily = json.loads(
            urllib.request.urlopen(f"{base}/api/daily?date=2026-10-22", timeout=30).read()
        )
        assert daily["synthesis_stale"] is True
        assert daily["synthesis_stale_reason"] == "count_mismatch"
        assert daily["synthesis_status"]["canonical_event_count"] == 22
        assert daily["synthesis_status"]["synthesis_event_count"] == 17

        agent = json.loads(
            urllib.request.urlopen(f"{base}/api/daily-brief?date=2026-10-22", timeout=30).read()
        )
        assert agent["synthesis_stale"] is True
        assert agent["synthesis_stale_reason"] == "count_mismatch"
        assert agent["synthesis_status"]["synthesis_event_count"] == 17
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_statistics_endpoint_uses_requested_date_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    server = create_server(host="127.0.0.1", port=0, store=store)
    expected = {
        "schema_version": "statistics-v1",
        "date_from": "2026-09-15",
        "date_to": "2026-09-21",
        "event_count": 9,
        "expense_total_cny": 88.5,
        "exercise_count": 2,
    }

    # 【2026-10-17】读接口已扩展：/api/stats 现在会传 scope / view / event_filter
    # （settled vs provisional 的读契约，以及看板「隐藏电脑前说话」的读层过滤）。
    # 这里的桩必须跟上新签名，否则测的是过期契约。
    def fake_stats(job_root, *, date_from, date_to, scope="provisional", view="all", event_filter=None):
        assert Path(job_root) == store.root
        assert date_from == "2026-09-15"
        assert date_to == "2026-09-21"
        assert scope in {"provisional", "settled"}
        return expected

    monkeypatch.setattr("life_logger.local_web.build_range_statistics", fake_stats)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.loads(
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}"
                "/api/stats?from=2026-09-15&to=2026-09-21"
            ).read()
        )
        assert payload == expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_p4_batch_d_local_reminder_api_persists_schedule(tmp_path: Path) -> None:
    class FakeNotifier:
        name = "fake-local"

        def notify(self, *, title: str, body: str) -> bool:
            del title, body
            return True

    store = JobStore(tmp_path / "local-web", recover=False)
    store.save_task_state(
        task_key="todo-local-1",
        status="pending",
        title="提交周报",
        event_id="todo-local-1",
    )
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        reminder_notifier=FakeNotifier(),
    )
    # [2026-10-13] `/api/reminders` 现在优先从 OpenClaw 取真待办（它才是事实源）。
    # 本测试只验证「本地 reminders 的 schedule/status 持久化」这一条路径，必须把
    # OpenClaw 分支隔离掉 —— 否则在开发机上（8791 真在跑）它会返回 OpenClaw 的
    # 任务，把这里准备的本地 todo-local-1 挤掉，断言就永远对不上。
    server._openclaw_reminder_tasks = lambda: []  # type: ignore[method-assign]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        initial = json.loads(
            urllib.request.urlopen(f"{base}/api/reminders").read()
        )
        assert initial["backend"] == "local"
        assert initial["reminders"][0]["task_key"] == "todo-local-1"
        assert initial["reminders"][0]["status"] == "unscheduled"

        request = urllib.request.Request(
            f"{base}/api/reminders/schedule",
            data=json.dumps(
                {
                    "task_key": "todo-local-1",
                    "title": "提交周报",
                    "remind_at": "2099-09-22T09:00:00+08:00",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        scheduled = json.loads(
            urllib.request.urlopen(request).read()
        )["reminder"]
        assert scheduled["status"] == "scheduled"
        assert scheduled["backend"] == "local"

        persisted = json.loads((store.root / "reminders.json").read_text())
        assert persisted["reminders"]["todo-local-1"]["status"] == "scheduled"

        # Test POST /api/reminders/status marking as completed
        status_req = urllib.request.Request(
            f"{base}/api/reminders/status",
            data=json.dumps(
                {
                    "task_key": "todo-local-1",
                    "status": "completed",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        status_resp = json.loads(urllib.request.urlopen(status_req).read())
        assert status_resp["ok"] is True
        assert status_resp["reminder"]["status"] == "completed"

        # Test POST /api/reminders/status marking as dismissed
        status_req2 = urllib.request.Request(
            f"{base}/api/reminders/status",
            data=json.dumps(
                {
                    "task_key": "todo-local-1",
                    "status": "dismissed",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        status_resp2 = json.loads(urllib.request.urlopen(status_req2).read())
        assert status_resp2["ok"] is True
        assert status_resp2["reminder"]["status"] == "dismissed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_openclaw_web_endpoints(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    config_path = tmp_path / "local-config.json"
    config_path.write_text(json.dumps({}), encoding="utf-8")
    server = create_server(host="127.0.0.1", port=0, store=store, config_path=config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        health = json.loads(urllib.request.urlopen(f"{base}/api/openclaw/health").read())
        assert "ready" in health

        tasks = json.loads(urllib.request.urlopen(f"{base}/api/openclaw/tasks").read())
        assert "connected" in tasks

        sync_req = urllib.request.Request(
            f"{base}/api/openclaw/sync",
            data=b'{"events": []}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        sync_resp = json.loads(urllib.request.urlopen(sync_req).read())
        assert sync_resp["ok"] is True
        assert sync_resp["synced_count"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)



def test_openclaw_tasks_exposes_real_timestamp_contract_without_request_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(OpenClawBridgeClient, "is_healthy", lambda self: True)

    def fake_dashboard(self):
        return {
            "total_open": 2,
            "raw_count": 2,
            "wechat_matters": [{
                "id": "oc_task_a", "title": "微信任务", "status": "pending",
                "updated_at": "2026-10-23T12:45:30.402Z",
            }],
            "general_todos": [{
                "id": "oc_task_b", "title": "通用任务", "status": "pending",
                "updated_at": "2026-10-22T08:00:00.000Z",
            }],
            "life_logger_todos": [],
        }

    monkeypatch.setattr(OpenClawBridgeClient, "get_unified_dashboard_todos", fake_dashboard)
    store = JobStore(tmp_path / "local-web", recover=False)
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    server = create_server(host="127.0.0.1", port=0, store=store, config_path=config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        payload = json.loads(urllib.request.urlopen(f"{base}/api/openclaw/tasks").read())
        assert payload["connected"] is True
        assert payload["total_open"] == 2
        assert [task["id"] for task in payload["wechat_matters"]] == ["oc_task_a"]
        assert payload["source_updated_at"] == "2026-10-23T12:45:30.402Z"
        assert payload["last_sync"] is None
        assert payload["last_sync_at"] == payload["source_updated_at"]
        assert payload["last_sync_basis"] == "source_updated_at"
        assert payload["generated_at"] is None
        assert "不拿请求时间冒充" in payload["last_sync_reason"]
        assert "不拿请求时间冒充" in payload["generated_at_reason"]
        contract = payload["read_contract"]
        assert contract["schema_version"] == "openclaw-task-read-contract-v1"
        assert contract["scope"] == "openclaw_tasks"
        assert contract["last_sync_available"] is False
        assert contract["source_updated_at_available"] is True

        # Same contract is exposed by the other task endpoint.
        tasks_payload = json.loads(urllib.request.urlopen(f"{base}/api/tasks").read())
        assert tasks_payload["openclaw"]["source_updated_at"] == payload["source_updated_at"]
        assert tasks_payload["openclaw"]["last_sync_at"] == payload["last_sync_at"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_openclaw_tasks_without_updated_at_returns_null_and_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(OpenClawBridgeClient, "is_healthy", lambda self: True)

    def fake_dashboard(self):
        return {
            "total_open": 1,
            "raw_count": 1,
            "wechat_matters": [{"id": "oc_task_a", "title": "无更新时间", "status": "pending"}],
            "general_todos": [],
            "life_logger_todos": [],
        }

    monkeypatch.setattr(OpenClawBridgeClient, "get_unified_dashboard_todos", fake_dashboard)
    store = JobStore(tmp_path / "local-web", recover=False)
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    server = create_server(host="127.0.0.1", port=0, store=store, config_path=config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        payload = json.loads(urllib.request.urlopen(f"{base}/api/openclaw/tasks").read())
        assert payload["source_updated_at"] is None
        assert payload["last_sync"] is None
        assert payload["last_sync_at"] is None
        assert "updated_at" in payload["source_updated_at_reason"]
        assert payload["generated_at"] is None
        assert payload["read_contract"]["source_updated_at_available"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_openclaw_offline_reports_metadata_reasons_not_current_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(OpenClawBridgeClient, "is_healthy", lambda self: False)
    store = JobStore(tmp_path / "local-web", recover=False)
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    server = create_server(host="127.0.0.1", port=0, store=store, config_path=config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        payload = json.loads(urllib.request.urlopen(f"{base}/api/openclaw/tasks").read())
        assert payload["connected"] is False
        assert payload["source_updated_at"] is None
        assert payload["last_sync_at"] is None
        assert "OpenClaw bridge unreachable" in payload["last_sync_reason"]
        assert payload["read_contract"]["source_updated_at_available"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _write_watch_manifest(root: Path, *, with_file: bool = True) -> Path:
    session = root / "watch-sessions" / "watch-test" / "session-a"
    segment_dir = session / "segments"
    segment_dir.mkdir(parents=True)
    segment = {
        "sequence": 0,
        "start_offset_ms": 0,
        "duration_ms": 60_000,
        "file": "segment-000000.m4a",
        "received_at": "2026-10-23T23:34:16+08:00",
    }
    manifest = {
        "schema_version": "watch-session-v1",
        "device_id": "watch-test",
        "session_id": "session-a",
        "started_at": "2026-10-23T13:58:31.104000+00:00",
        "state": "complete",
        "segments": [segment],
    }
    (session / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if with_file:
        path = segment_dir / segment["file"]
        path.write_bytes(b"fake-m4a")
        stamp = datetime.fromisoformat("2026-10-23T23:34:15+08:00").timestamp()
        os.utime(path, (stamp, stamp))
    return session


def test_watch_last_segment_received_prefers_real_file_mtime(tmp_path: Path) -> None:
    _write_watch_manifest(tmp_path)
    result = _watch_last_segment_received(tmp_path / "watch-sessions", "2026-10-23")
    assert result["last_segment_received_at"] == "2026-10-23T23:34:15+08:00"
    assert result["last_segment_received_source"] == "segment_file_mtime"
    assert result["last_segment"]["sequence"] == 0
    assert result["last_recording_end_at"] == "2026-10-23T21:59:31+08:00"


def test_watch_last_segment_received_falls_back_to_coverage_tail(tmp_path: Path) -> None:
    _write_watch_manifest(tmp_path, with_file=False)
    result = _watch_last_segment_received(tmp_path / "watch-sessions", "2026-10-23")
    assert result["last_segment_received_at"] == "2026-10-23T21:59:31+08:00"
    assert result["last_segment_received_source"] == "coverage_tail"
    assert "coverage" in result["last_segment_received_reason"]


def test_watch_last_segment_received_returns_null_without_evidence(tmp_path: Path) -> None:
    result = _watch_last_segment_received(tmp_path / "empty", "2026-10-23")
    assert result["last_segment_received_at"] is None
    assert result["last_segment_received_source"] is None
    assert "不拿处理水位" in result["last_segment_received_reason"]


def test_dashboard_daily_exposes_last_segment_received_at(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _write_watch_manifest(root)
    store = JobStore(root, recover=False)
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    server = create_server(host="127.0.0.1", port=0, store=store, config_path=config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        payload = json.loads(
            urllib.request.urlopen(
                f"{base}/api/dashboard/daily?date=2026-10-23", timeout=30
            ).read()
        )
        assert payload["last_segment_received_at"] == "2026-10-23T23:34:15+08:00"
        assert payload["dashboard"]["last_segment_received_at"] == payload["last_segment_received_at"]
        assert payload["last_segment_received_source"] == "segment_file_mtime"
        assert payload["last_recording_end_at"] == "2026-10-23T21:59:31+08:00"
        assert "processing_watermark" not in payload["last_segment_received_source"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_synthesis_staleness_marks_count_mismatch_without_regeneration() -> None:
    result = _synthesis_staleness(
        {"date": "2026-10-22", "event_count": 22, "generated_at": "2026-10-24T14:24:41+08:00"},
        {"event_count": 17, "generated_at": "2026-10-23T02:38:37+08:00",
         "event_count_basis": "caller-supplied event_view events after read-layer filtering"},
    )
    assert result["synthesis_stale"] is True
    assert result["synthesis_stale_reason"] == "count_mismatch"
    status = result["synthesis_status"]
    assert status["canonical_event_count"] == 22
    assert status["synthesis_event_count"] == 17
    assert status["count_mismatch"] is True
    assert status["known"] is True
    assert status["synthesis_generated_at"] == "2026-10-23T02:38:37+08:00"


def test_synthesis_staleness_is_false_when_counts_match() -> None:
    result = _synthesis_staleness(
        {"date": "2026-10-23", "event_count": 18},
        {"event_count": 18, "generated_at": "2026-10-24T01:30:16+08:00"},
    )
    assert result["synthesis_stale"] is False
    assert result["synthesis_stale_reason"] is None
    assert result["synthesis_status"]["count_mismatch"] is False


def test_synthesis_staleness_is_unknown_when_fields_missing() -> None:
    result = _synthesis_staleness({"event_count": 18}, {"generated_at": "2026-10-24T01:30:16+08:00"})
    assert result["synthesis_stale"] is None
    assert result["synthesis_stale_reason"] == "missing_event_count"
    assert result["synthesis_status"]["known"] is False
    missing = _synthesis_staleness({"event_count": 18}, None)
    assert missing["synthesis_stale"] is None
    assert missing["synthesis_stale_reason"] == "synthesis_missing"


def test_asr_jobs_are_bounded_by_a_memory_sized_gate(tmp_path: Path) -> None:
    """Unbounded parallel ASR is what froze this 16 GB machine on 2026-10-02.

    Each job loads the 2.0 GB checkpoint into ~4.6 GB resident, so a burst of ten jobs
    demanded ~46 GB, swapped the host, and every job in the burst ended up "interrupted".
    The bound is memory-sized, not core-sized: two gates ≈ 9.2 GB, leaving the rest of RAM
    to the system. If this number is raised, the memory headroom has to be raised with it.
    """
    store = JobStore(tmp_path / "local-web", recover=False)
    gate = store._job_gates

    # A BoundedSemaphore of 2: two jobs may hold a gate, and a third must wait.
    assert gate.acquire(timeout=0) is True
    assert gate.acquire(timeout=0) is True
    assert gate.acquire(timeout=0) is False
    gate.release()
    assert gate.acquire(timeout=0) is True
    gate.release()
    gate.release()

    # Releasing more times than were acquired is a programming error, not a silent widening.
    with pytest.raises(ValueError):
        gate.release()


def test_worker_releases_its_gate_when_the_job_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate must come back even when the job raises; otherwise the queue stops forever."""
    # The cross-process ASR slot table lives in the real output/local-web directory, so a
    # test that runs while the live service holds both slots would wait its full 600 s
    # timeout inside the worker. Point this test at its own slot table.
    monkeypatch.setattr(
        "life_logger.asr_slots.DEFAULT_STATE_PATH",
        tmp_path / "asr-slots.json",
    )
    monkeypatch.setattr("life_logger.asr_slots.available_memory_gb", lambda: 16.0)
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    store.add_file(
        job_id,
        filename="probe.m4a",
        relative_path="probe.m4a",
        stream=io.BytesIO(b"fake-m4a-bytes"),
    )

    def exploding_run_web_job(job_dir: Path, config: dict, emit) -> None:
        del job_dir, config, emit
        raise RuntimeError("asr exploded")

    monkeypatch.setattr("scripts.local_jobs.run_web_job", exploding_run_web_job)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({}), encoding="utf-8")

    store.start_job(job_id, config_path)
    with store._lock:
        worker = store._active.get(job_id)
    if worker is not None:
        worker.join(timeout=10)

    # Both gates are available again: the failed job did not leak one.
    assert store._job_gates.acquire(timeout=0) is True
    assert store._job_gates.acquire(timeout=0) is True
    store._job_gates.release()
    store._job_gates.release()


def test_worker_fails_closed_when_shared_asr_slot_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local semaphore is not enough: never start ASR if the cross-process gate denied it."""
    monkeypatch.setattr(
        "life_logger.asr_slots.DEFAULT_STATE_PATH",
        tmp_path / "asr-slots.json",
    )
    monkeypatch.setattr(
        "life_logger.asr_slots.acquire_asr_slot",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "scripts.local_jobs.run_web_job",
        lambda *args, **kwargs: pytest.fail("ASR must not run without a shared slot"),
    )
    monkeypatch.setattr("scripts.local_jobs.validate_web_config", lambda _: None)
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    store.add_file(
        job_id,
        filename="probe.m4a",
        relative_path="probe.m4a",
        stream=io.BytesIO(b"fake-m4a-bytes"),
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({}), encoding="utf-8")

    store.start_job(job_id, config_path)
    with store._lock:
        worker = store._active.get(job_id)
    if worker is not None:
        worker.join(timeout=10)

    state = store.read(job_id)
    assert state["status"] == "failed"
    assert "ASR resource slot" in state["error"]


def test_watch_direct_exemption_is_not_a_blanket_lan_write_pass(tmp_path: Path) -> None:
    """Anything on the same network used to be able to create sessions and read manifests
    because the Watch exemption ran before the bearer check and covered both. The exemption is
    now limited to the three calls a recording Watch actually makes, and those require the
    compiled-in Watch credential instead of "the caller is on a private address".
    """
    from life_logger.local_web import _Handler

    def exemption_for(path: str, method: str) -> bool:
        handler = _Handler.__new__(_Handler)
        handler.path = path
        handler.command = method
        return handler._is_watch_direct_request()

    base = "/api/watch-sessions/watch-abc"

    # The three calls a recording Watch makes: register, push a segment, close the session.
    assert exemption_for("/api/watch-sessions", "POST") is True
    assert exemption_for(f"{base}/session-1/segments/0", "PUT") is True
    assert exemption_for(f"{base}/session-1/complete", "POST") is True

    # Reading a manifest is a read of the user's audio metadata: no exemption.
    assert exemption_for(f"{base}/session-1", "GET") is False
    assert exemption_for("/api/watch-sessions", "GET") is False
    # Registering is exempt, but only as a write.
    assert exemption_for("/api/watch-sessions", "DELETE") is False

    # The connectivity probe is exempt through `_is_watch_health_probe`, not through this
    # method — keeping it here too would make the exemption look broader than it is.
    assert exemption_for("/api/watch-health", "GET") is False

    # Unrelated routes were never exempt.
    assert exemption_for("/api/jobs", "POST") is False
    assert exemption_for("/api/privacy", "GET") is False


def test_watch_credential_is_what_the_watch_routes_check(tmp_path: Path) -> None:
    """The three Watch routes accept a compiled-in credential instead of a typed token.

    The route that must never break is the segment PUT: without it a recording cannot sync at
    all. Its exemption is pinned here, along with the fact that the credential is what the
    exemption checks (not the source address).
    """
    from life_logger.local_web import _Handler, _watch_upload_key

    def route_is_watch_exempt(path: str, method: str) -> bool:
        handler = _Handler.__new__(_Handler)
        handler.path = path
        handler.command = method
        return handler._is_watch_direct_request()

    base = "/api/watch-sessions/watch-abc/session-1"
    good = "lifelogger-watch-upload-key-v1"

    # The route that must never break: pushing a segment.
    assert route_is_watch_exempt(f"{base}/segments/0", "PUT") is True
    assert route_is_watch_exempt(f"{base}/complete", "POST") is True
    assert route_is_watch_exempt("/api/watch-sessions", "POST") is True
    assert route_is_watch_exempt(base, "GET") is False


    # With no secrets.json the value the Watch app ships with is the one expected.
    assert _watch_upload_key(tmp_path) == good
    (tmp_path / "secrets.json").write_text(
        json.dumps({"watch_upload_key": "rotated-key"}), encoding="utf-8"
    )
    assert _watch_upload_key(tmp_path) == "rotated-key"
    (tmp_path / "secrets.json").write_text(
        json.dumps({"watch_upload_key": "   "}), encoding="utf-8"
    )
    assert _watch_upload_key(tmp_path) == good


def test_watch_reception_exposes_sanitized_formal_asr_progress(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        access_token="test-watch-progress-token",
    )
    server.watch_session_store.start_session(
        device_id="watch-test",
        session_id="session-progress",
        started_at="2026-09-27T10:00:00+08:00",
    )
    server.watch_formal_asr.summary_for_session = lambda *_args: {
        "status": "processing",
        "counts": {"pending": 0, "processing": 0, "completed": 0, "no_speech": 0, "failed": 1},
        "transcript_count": 0,
        "transcripts": [],
        "automatic_retry": {
            "failed_segments": 1,
            "waiting_segments": 1,
            "next_retry_in_seconds": 47,
            "error": "private retry error",
        },
    }
    job_id = store.create_job()
    server.watch_session_store.mark_completion_job("watch-test", "session-progress", job_id)
    status_path = store.status_path(job_id)
    job_status = json.loads(status_path.read_text(encoding="utf-8"))
    job_status["status"] = "running"
    job_status["files"].append({"status": "completed", "outcome": "no_speech"})
    status_path.write_text(json.dumps(job_status), encoding="utf-8")
    checkpoint = store.job_path(job_id) / "files" / "abc" / "asr" / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps(
            {
                "status": "running",
                "source": "/private/audio.m4a",
                "model_path": "/private/model",
                "completed_windows": 2,
                "total_windows": 5,
                "transcripts": [
                    {
                        "start_offset_seconds": 12.5,
                        "end_offset_seconds": 18.0,
                        "text": "正在处理的正式转写句子",
                        "source_file": "/private/audio.m4a",
                        "api_key": "must-not-leak",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    extraction_path = (
        store.job_path(job_id)
        / "files"
        / "abc"
        / "result"
        / "context-extraction.json"
    )
    extraction_path.parent.mkdir(parents=True)
    extraction_path.write_text(
        json.dumps(
            {
                "status": "degraded",
                "kev": {
                    "status": "degraded",
                    "errors": [{"error": "private failure detail"}],
                    "model": "private-model-name",
                },
                "counts": {
                    "formal_events": 2,
                    "pending_events": 1,
                    "pending_sources": 3,
                },
                "formal_events": [{"title": "private event text"}],
                "source_path": "/private/job/source.wav",
                "error": "private raw error",
            }
        ),
        encoding="utf-8",
    )
    malformed_extraction = (
        store.job_path(job_id)
        / "files"
        / "zzz"
        / "result"
        / "context-extraction.json"
    )
    malformed_extraction.parent.mkdir(parents=True)
    malformed_extraction.write_bytes(b"\xff\xfe")

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/watch-reception",
            headers={"Authorization": "Bearer test-watch-progress-token"},
        )
        payload = json.loads(urllib.request.urlopen(request).read())
        session = payload["sessions"][0]
        assert "preview" not in session
        assert session["completion_job_id"] == job_id
        assert session["formal_job"]["job_id"] == job_id
        assert session["formal_job"]["status"] == "running"
        assert session["formal_job"]["no_speech_count"] == 1
        assert "跳过声源归属与事件提取" in session["formal_job"]["no_speech_reason"]
        assert session["formal_job"]["asr_progress"] == {
            "completed_windows": 2,
            "total_windows": 5,
            "unit": "windows",
        }
        assert session["formal_job"]["transcript_count"] == 1
        assert session["formal_job"]["context_extraction"] == {
            "status": "degraded",
            "kev_status": "degraded",
            "counts": {
                "formal_events": 2,
                "pending_events": 1,
                "pending_sources": 3,
            },
        }
        assert session["formal_job"]["latest_transcript_rows"] == [
            {
                "start_offset_seconds": 12.5,
                "end_offset_seconds": 18.0,
                "text": "正在处理的正式转写句子",
            }
        ]
        assert session["formal_asr"]["automatic_retry"] == {
            "failed_segments": 1,
            "waiting_segments": 1,
            "next_retry_in_seconds": 47,
        }
        encoded = json.dumps(payload, ensure_ascii=False)
        assert "/private" not in encoded
        assert "must-not-leak" not in encoded
        assert "private failure detail" not in encoded
        assert "private-model-name" not in encoded
        assert "private event text" not in encoded
        assert "private raw error" not in encoded
        assert "private retry error" not in encoded

        job_status["status"] = "completed"
        status_path.write_text(json.dumps(job_status), encoding="utf-8")
        completed_request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/watch-reception",
            headers={"Authorization": "Bearer test-watch-progress-token"},
        )
        completed_payload = json.loads(
            urllib.request.urlopen(completed_request).read()
        )
        completed_job = completed_payload["sessions"][0]["formal_job"]
        assert completed_job["status"] == "completed"
        assert "transcript_artifact_path" not in completed_job
        assert completed_job["transcript_count"] == 1
        transcript_artifact = "files/abc/result/2026-10-06/transcript.json"
        transcript_file = store.job_path(job_id) / transcript_artifact
        transcript_file.parent.mkdir(parents=True)
        transcript_file.write_bytes(b"")
        job_status["results"] = [transcript_artifact]
        status_path.write_text(json.dumps(job_status), encoding="utf-8")
        completed_request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/watch-reception",
            headers={"Authorization": "Bearer test-watch-progress-token"},
        )
        completed_payload = json.loads(urllib.request.urlopen(completed_request).read())
        completed_job = completed_payload["sessions"][0]["formal_job"]
        assert completed_job["transcript_artifact_path"] == transcript_artifact
        assert completed_job["latest_transcript_rows"] == [
            {
                "start_offset_seconds": 12.5,
                "end_offset_seconds": 18.0,
                "text": "正在处理的正式转写句子",
            }
        ]

        # A result path must be canonical, stay inside this Job, and name an
        # existing final result transcript. Never accept arbitrary Job metadata.
        job_status["results"] = [
            "../outside/files/abc/result/2026-10-06/transcript.json",
            "files/abc/asr/2026-10-06/transcript.json",
            "files/abc/result/2026-10-06/not-transcript.json",
        ]
        status_path.write_text(json.dumps(job_status), encoding="utf-8")
        sanitized_request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/watch-reception",
            headers={"Authorization": "Bearer test-watch-progress-token"},
        )
        sanitized_payload = json.loads(urllib.request.urlopen(sanitized_request).read())
        assert "transcript_artifact_path" not in sanitized_payload["sessions"][0]["formal_job"]

        # Retry metadata is exposed only as a sanitized waiting state. Internal error
        # details and filesystem paths remain private.
        job_status["status"] = "failed"
        job_status["error"] = "ASR resource slot unavailable: /private/slot.lock"
        job_status["automatic_retry"] = {
            "attempts": 3,
            "next_attempt_at": "2026-10-05T12:00:00+08:00",
        }
        status_path.write_text(json.dumps(job_status), encoding="utf-8")
        retry_request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/watch-reception",
            headers={"Authorization": "Bearer test-watch-progress-token"},
        )
        retry_payload = json.loads(urllib.request.urlopen(retry_request).read())
        retry_job = retry_payload["sessions"][0]["formal_job"]
        assert retry_job["automatic_retry"] == {
            "status": "waiting",
            "attempts": 3,
            "next_attempt_at": "2026-10-05T12:00:00+08:00",
            "reason": "ASR 资源繁忙，等待自动重试",
        }
        assert "/private" not in json.dumps(retry_payload, ensure_ascii=False)

        job_status["status"] = "completed"
        job_status["context_auto_retry"] = {
            "status": "scheduled",
            "attempts": 0,
            "max_attempts": 1,
            "next_attempt_at": "2026-10-06T12:00:30+08:00",
            "input_fingerprint": "private-fingerprint",
            "file_ids": ["private-file-id"],
            "failure_kinds": ["http_error"],
        }
        status_path.write_text(json.dumps(job_status), encoding="utf-8")
        context_retry_request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/watch-reception",
            headers={"Authorization": "Bearer test-watch-progress-token"},
        )
        context_retry_payload = json.loads(
            urllib.request.urlopen(context_retry_request).read()
        )
        context_retry = context_retry_payload["sessions"][0]["formal_job"][
            "context_auto_retry"
        ]
        assert context_retry == {
            "status": "scheduled",
            "attempts": 0,
            "max_attempts": 1,
            "next_attempt_at": "2026-10-06T12:00:30+08:00",
        }
        assert "private-fingerprint" not in json.dumps(context_retry_payload)
        assert "private-file-id" not in json.dumps(context_retry_payload)

        job_status["status"] = "failed"
        job_status["automatic_retry"]["next_attempt_at"] = "private / not-a-date"
        status_path.write_text(json.dumps(job_status), encoding="utf-8")
        malformed_retry_request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/watch-reception",
            headers={"Authorization": "Bearer test-watch-progress-token"},
        )
        malformed_retry_payload = json.loads(
            urllib.request.urlopen(malformed_retry_request).read()
        )
        malformed_retry = malformed_retry_payload["sessions"][0]["formal_job"]["automatic_retry"]
        assert malformed_retry == {
            "status": "waiting",
            "attempts": 3,
            "reason": "ASR 资源繁忙，等待自动重试",
        }
        assert "private / not-a-date" not in json.dumps(malformed_retry_payload, ensure_ascii=False)
    finally:
        server.shutdown()
        server.server_close()


def test_watch_reception_exposes_reserved_draft_formal_job(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        access_token="test-watch-reserved-job-token",
    )
    server.watch_session_store.start_session(
        device_id="watch-test",
        session_id="session-reserved",
        started_at="2026-09-27T10:00:00+08:00",
    )
    job_id = store.create_watch_pending_job(
        device_id="watch-test",
        session_id="session-reserved",
        start_at="2026-09-27T10:00:00+08:00",
        timezone_name="Asia/Shanghai",
    )
    server.watch_session_store.mark_completion_job(
        "watch-test", "session-reserved", job_id
    )
    server.watch_session_store.start_session(
        device_id="watch-test",
        session_id="session-reserved-missing-job",
        started_at="2026-09-27T10:01:00+08:00",
    )
    missing_job_id = "job-reserved-missing-1234"
    server.watch_session_store.mark_completion_job(
        "watch-test", "session-reserved-missing-job", missing_job_id
    )

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/watch-reception",
            headers={"Authorization": "Bearer test-watch-reserved-job-token"},
        )
        payload = json.loads(urllib.request.urlopen(request).read())
        sessions = {item["session_id"]: item for item in payload["sessions"]}
        session = sessions["session-reserved"]

        assert session["job_id"] is None
        assert session["completion_job_id"] == job_id
        assert session["formal_job"]["job_id"] == job_id
        assert session["formal_job"]["status"] == "draft"
        assert session["formal_job"]["stage"] == "waiting_for_files"
        unavailable = sessions["session-reserved-missing-job"]
        assert unavailable["completion_job_id"] == missing_job_id
        assert unavailable["formal_job"] == {"status": "unavailable"}
    finally:
        server.shutdown()
        server.server_close()


def test_local_http_api_queues_context_recovery_on_the_existing_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=config_path,
    )
    calls: list[tuple[str, Path, Path | None]] = []

    def fake_retry(
        requested_job_id: str,
        requested_config_path: Path,
        *,
        secrets_path: Path | None = None,
    ) -> dict[str, str]:
        calls.append((requested_job_id, requested_config_path, secrets_path))
        return {"job_id": requested_job_id, "status": "completed"}

    monkeypatch.setattr(store, "retry_context_extraction", fake_retry)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/jobs/"
            f"{job_id}/retry-context-extraction",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read())
            assert response.status == 202

        assert payload == {"job_id": job_id, "status": "completed"}
        assert calls == [
            (job_id, config_path, store.root / "secrets.json")
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_watch_formal_asr_scan_is_single_flight_and_off_coordinator_thread() -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []
    coordinator_ids: list[int] = []
    coordinator_results: list[bool] = []

    class BlockingProcessor:
        def scan_once(self) -> bool:
            calls.append(threading.get_ident())
            entered.set()
            release.wait(timeout=2)
            return True

    server = object.__new__(LocalWebServer)
    server._watch_formal_scan_lock = threading.Lock()
    # The service constructor used to store its long-lived caller thread here as a
    # placeholder. A different coordinator thread then mistook it for an active scan.
    server._watch_formal_scan_thread = threading.current_thread()
    server.watch_formal_asr = BlockingProcessor()

    def schedule_from_coordinator() -> None:
        coordinator_ids.append(threading.get_ident())
        coordinator_results.append(server._schedule_watch_formal_asr_scan())

    coordinator = threading.Thread(
        target=schedule_from_coordinator,
        name="watch-formal-asr-coordinator",
    )
    coordinator.start()
    coordinator.join(timeout=1)
    assert not coordinator.is_alive()
    assert coordinator_results == [False]
    assert entered.wait(timeout=1)
    assert calls == [server._watch_formal_scan_thread.ident]
    assert calls[0] != coordinator_ids[0]
    assert server._schedule_watch_formal_asr_scan() is True
    assert len(calls) == 1

    release.set()
    server._watch_formal_scan_thread.join(timeout=2)
    assert not server._watch_formal_scan_thread.is_alive()
    assert server._schedule_watch_formal_asr_scan() is False
    server._watch_formal_scan_thread.join(timeout=2)
    assert len(calls) == 2
    assert all(call != threading.current_thread().ident for call in calls)


def test_job_finished_persists_backoff_only_for_known_asr_slot_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    monkeypatch.setattr("life_logger.local_web._now", lambda: 1_800_000_000.0)

    store.update_from_worker(
        job_id,
        {
            "type": "job_finished",
            "status": "failed",
            "error": "ASR resource slot unavailable; job remains retryable",
        },
    )
    first = store.read(job_id)["automatic_retry"]
    assert first["attempts"] == 1
    assert first["next_attempt_at"] == "2027-01-15T16:00:30+08:00"

    store.update_from_worker(
        job_id,
        {
            "type": "job_finished",
            "status": "failed",
            "error": "ASR resource slot unavailable: busy",
        },
    )
    second = store.read(job_id)["automatic_retry"]
    assert second["attempts"] == 2
    assert second["next_attempt_at"] == "2027-01-15T16:01:00+08:00"

    store.update_from_worker(
        job_id,
        {"type": "job_finished", "status": "failed", "error": "model load failed"},
    )
    assert "automatic_retry" not in store.read(job_id)


@pytest.mark.parametrize(
    ("session_state", "linked_job", "retry_due", "expected_start"),
    [
        ("complete", "target", True, True),
        ("complete", "target", False, False),
        ("receiving", "target", True, False),
        ("complete", "other", True, False),
    ],
)
def test_watch_live_loop_retries_only_due_complete_linked_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_state: str,
    linked_job: str,
    retry_due: bool,
    expected_start: bool,
) -> None:
    root = tmp_path / "local-web"
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("life_logger.local_web._now", lambda: 1_800_000_000.0)

    store = JobStore(root, recover=False)
    job_id = store.create_job()
    store.update_from_worker(
        job_id,
        {
            "type": "job_finished",
            "status": "failed",
            "error": "ASR resource slot unavailable; job remains retryable",
        },
    )
    state = json.loads(store.status_path(job_id).read_text(encoding="utf-8"))
    state["automatic_retry"]["next_attempt_at"] = (
        "2027-01-15T15:59:59+08:00"
        if retry_due
        else "2027-01-15T16:00:30+08:00"
    )
    store.status_path(job_id).write_text(json.dumps(state), encoding="utf-8")

    other_job_id = store.create_job() if linked_job == "other" else job_id
    # Re-open the JSON-backed store to model a service restart between failure and retry.
    store = JobStore(root, recover=False)
    started: list[str] = []

    def fake_start(
        candidate_job_id: str,
        candidate_config_path: Path,
        *,
        secrets_path: Path | None = None,
    ) -> dict[str, object]:
        started.append(candidate_job_id)
        return store.read(candidate_job_id)

    monkeypatch.setattr(store, "start_job", fake_start)

    class OnePassStop:
        def __init__(self) -> None:
            self.waits = 0

        def wait(self, timeout: float) -> bool:
            self.waits += 1
            return self.waits > 1

    class WatchSessions:
        def finalize_completion_intents(self) -> list[dict[str, object]]:
            return []

        def abandon_empty_sessions(self, *, apply: bool) -> list[dict[str, object]]:
            return []

        def list_session_summaries(self, *, limit: int | None = None) -> list[dict[str, object]]:
            return [
                {
                    "device_id": "watch-test",
                    "session_id": "session-retry",
                    "state": session_state,
                    "job_id": job_id if linked_job == "target" else other_job_id,
                }
            ]

    server = object.__new__(LocalWebServer)
    server._watch_live_stop = OnePassStop()
    server.config_path = config_path
    server.store = store
    server.watch_session_store = WatchSessions()
    server._schedule_watch_formal_asr_scan = lambda: False

    server._watch_live_loop()

    assert started == ([job_id] if expected_start else [])


def test_watch_live_loop_only_finalizes_explicit_completion_intents(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")

    class OnePassStop:
        def __init__(self) -> None:
            self.waits = 0

        def wait(self, timeout: float) -> bool:
            self.waits += 1
            return self.waits > 1

    class WatchSessions:
        completion_calls = 0
        abandoned_calls = 0
        superseded_calls = 0

        def finalize_completion_intents(self) -> list[dict[str, object]]:
            self.completion_calls += 1
            return []

        def finalize_superseded_sessions(self, *, apply: bool) -> list[dict[str, object]]:
            self.superseded_calls += 1
            raise AssertionError("unattended loop must not infer Watch completion")

        def abandon_empty_sessions(self, *, apply: bool) -> list[dict[str, object]]:
            self.abandoned_calls += 1
            return []

        def list_session_summaries(self, *, limit: int | None = None) -> list[dict[str, object]]:
            return []

    watch_sessions = WatchSessions()
    server = object.__new__(LocalWebServer)
    server._watch_live_stop = OnePassStop()
    server.config_path = config_path
    server.store = object()
    server.watch_session_store = watch_sessions
    server._schedule_watch_formal_asr_scan = lambda: False

    server._watch_live_loop()

    assert watch_sessions.completion_calls == 1
    assert watch_sessions.abandoned_calls == 1
    assert watch_sessions.superseded_calls == 0


def test_watch_live_loop_adopts_persisted_legacy_slot_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "local-web"
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("life_logger.local_web._now", lambda: 1_800_000_000.0)

    store = JobStore(root, recover=False)
    job_id = store.create_job()
    store.update_from_worker(
        job_id,
        {
            "type": "job_finished",
            "status": "failed",
            "error": "ASR resource slot unavailable; job remains retryable",
        },
    )
    status_path = store.status_path(job_id)
    state = json.loads(status_path.read_text(encoding="utf-8"))
    state.pop("automatic_retry")
    status_path.write_text(json.dumps(state), encoding="utf-8")

    # The old service persisted only the failure. The new service adopts it after restart.
    store = JobStore(root, recover=False)

    class OnePassStop:
        def __init__(self) -> None:
            self.waits = 0

        def wait(self, timeout: float) -> bool:
            self.waits += 1
            return self.waits > 1

    class WatchSessions:
        def finalize_completion_intents(self) -> list[dict[str, object]]:
            return []

        def abandon_empty_sessions(self, *, apply: bool) -> list[dict[str, object]]:
            return []

        def list_session_summaries(self, *, limit: int | None = None) -> list[dict[str, object]]:
            return [
                {
                    "device_id": "watch-test",
                    "session_id": "session-retry",
                    "state": "complete",
                    "job_id": job_id,
                }
            ]

    server = object.__new__(LocalWebServer)
    server._watch_live_stop = OnePassStop()
    server.config_path = config_path
    server.store = store
    server.watch_session_store = WatchSessions()
    server._schedule_watch_formal_asr_scan = lambda: False
    server._watch_live_loop()

    retry = store.read(job_id)["automatic_retry"]
    assert retry["attempts"] == 1
    assert retry["next_attempt_at"] == "2027-01-15T16:00:30+08:00"


def test_watch_live_loop_retry_runs_the_same_job_through_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "local-web"
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("life_logger.local_web._now", lambda: 1_800_000_000.0)
    monkeypatch.setattr("life_logger.asr_slots.acquire_asr_slot", lambda *a, **k: object())
    monkeypatch.setattr("life_logger.asr_slots.release_asr_slot", lambda _slot: None)
    monkeypatch.setattr("scripts.local_jobs.validate_web_config", lambda _config: None)

    pipeline_jobs: list[str] = []

    def fake_run_web_job(
        job_dir: Path,
        _config: dict[str, object],
        emit,
        *,
        child_pid_callback=None,
    ) -> None:
        job_id = job_dir.name
        pipeline_jobs.append(job_id)
        emit({"type": "job_started"})
        emit(
            {
                "type": "file_finished",
                "file_index": 0,
                "status": "completed",
                "results": [],
            }
        )
        emit({"type": "job_finished", "status": "completed", "error": None})

    monkeypatch.setattr("scripts.local_jobs.run_web_job", fake_run_web_job)

    store = JobStore(root, recover=False)
    job_id = store.create_job()
    store.add_file(
        job_id,
        filename="watch.m4a",
        relative_path="watch.m4a",
        stream=io.BytesIO(b"assembled Watch recording"),
        source_metadata={
            "capture_source": "apple-watch",
            "source_device": "watch-test",
            "source_session": "session-retry-worker",
        },
    )
    store.update_from_worker(
        job_id,
        {
            "type": "job_finished",
            "status": "failed",
            "error": "ASR resource slot unavailable; job remains retryable",
        },
    )
    state = json.loads(store.status_path(job_id).read_text(encoding="utf-8"))
    state["automatic_retry"]["next_attempt_at"] = "2027-01-15T15:59:59+08:00"
    store.status_path(job_id).write_text(json.dumps(state), encoding="utf-8")

    store = JobStore(root, recover=False)
    started_threads: list[threading.Thread] = []
    original_start = store.start_job

    def capture_start(
        candidate_job_id: str,
        candidate_config_path: Path,
        *,
        secrets_path: Path | None = None,
    ) -> dict[str, object]:
        with store._lock:
            result = original_start(
                candidate_job_id,
                candidate_config_path,
                secrets_path=secrets_path,
            )
            worker = store._active.get(candidate_job_id)
        if worker is not None:
            started_threads.append(worker)
        return result

    monkeypatch.setattr(store, "start_job", capture_start)

    class OnePassStop:
        def __init__(self) -> None:
            self.waits = 0

        def wait(self, timeout: float) -> bool:
            self.waits += 1
            return self.waits > 1

    class WatchSessions:
        def finalize_completion_intents(self) -> list[dict[str, object]]:
            return []

        def abandon_empty_sessions(self, *, apply: bool) -> list[dict[str, object]]:
            return []

        def list_session_summaries(self, *, limit: int | None = None) -> list[dict[str, object]]:
            return [
                {
                    "device_id": "watch-test",
                    "session_id": "session-retry-worker",
                    "state": "complete",
                    "job_id": job_id,
                }
            ]

    server = object.__new__(LocalWebServer)
    server._watch_live_stop = OnePassStop()
    server.config_path = config_path
    server.store = store
    server.watch_session_store = WatchSessions()
    server._schedule_watch_formal_asr_scan = lambda: False
    server._watch_live_loop()

    try:
        assert len(started_threads) == 1
    finally:
        for worker in started_threads:
            worker.join(timeout=3)
    assert len(started_threads) == 1
    assert not started_threads[0].is_alive()
    state = store.read(job_id)
    assert pipeline_jobs == [job_id]
    assert state["status"] == "completed"
    assert state["files"][0]["status"] == "completed"
    assert "automatic_retry" not in state
    assert [item["job_id"] for item in store.list()] == [job_id]


def _make_watch_job_with_audio(
    store: JobStore,
    session_id: str = "session-auto-context",
) -> tuple[str, str]:
    job_id = store.create_job()
    file_state = store.add_file(
        job_id,
        filename="watch.m4a",
        relative_path=f"Apple Watch/watch-test/{session_id}/recording.m4a",
        stream=io.BytesIO(b"watch audio for isolated auto-retry test"),
        source_metadata={
            "capture_source": "apple-watch",
            "source_device": "watch-test",
            "source_session": session_id,
        },
    )["file"]
    file_id = str(file_state["sha256"])
    return job_id, file_id


def test_job_finished_schedules_one_retry_only_for_new_transient_watch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id, file_id = _make_watch_job_with_audio(store)
    candidate = {
        "file_ids": [file_id],
        "input_fingerprint": "f" * 64,
        "failure_kinds": ["http_error"],
    }
    monkeypatch.setattr(
        "life_logger.context_auto_retry.inspect_retry_candidate",
        lambda *_args: candidate,
    )
    monkeypatch.setattr("life_logger.local_web._now", lambda: 1_800_000_000.0)

    store.update_from_worker(
        job_id,
        {"type": "job_finished", "status": "completed", "error": None},
    )

    retry = store.read(job_id)["context_auto_retry"]
    assert retry["status"] == "scheduled"
    assert retry["attempts"] == 0
    assert retry["max_attempts"] == 1
    assert retry["next_attempt_at"] == "2027-01-15T16:00:30+08:00"
    assert retry["file_ids"] == [file_id]


def test_manual_job_is_not_enrolled_for_context_auto_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job()
    store.add_file(
        job_id,
        filename="manual.m4a",
        relative_path="manual.m4a",
        stream=io.BytesIO(b"manual audio"),
    )
    monkeypatch.setattr(
        "life_logger.context_auto_retry.inspect_retry_candidate",
        lambda *_args: (_ for _ in ()).throw(AssertionError("manual Job must be skipped")),
    )

    store.update_from_worker(
        job_id,
        {"type": "job_finished", "status": "completed", "error": None},
    )

    assert "context_auto_retry" not in store.read(job_id)


def test_due_context_auto_retry_revalidates_and_starts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "local-web"
    store = JobStore(root, recover=False)
    job_id, file_id = _make_watch_job_with_audio(store, "session-due-auto-context")
    candidate = {
        "file_ids": [file_id],
        "input_fingerprint": "a" * 64,
        "failure_kinds": ["timeout"],
    }
    monkeypatch.setattr(
        "life_logger.context_auto_retry.inspect_retry_candidate",
        lambda *_args: candidate,
    )
    monkeypatch.setattr("life_logger.local_web._now", lambda: 1_800_000_000.0)
    store.update_from_worker(
        job_id,
        {"type": "job_finished", "status": "completed", "error": None},
    )
    state = store.read(job_id)
    state["context_auto_retry"]["next_attempt_at"] = "2027-01-15T15:59:59+08:00"
    store.status_path(job_id).write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(
        "life_logger.context_extraction_recovery.find_context_recovery_targets",
        lambda *_args: ([{"file_id": file_id}], 0),
    )
    monkeypatch.setattr(
        "life_logger.local_pipeline.local_pipeline_env",
        lambda _config: {"DEEPSEEK_API_KEY": "test-key"},
    )
    worker_started = threading.Event()
    store._context_recovery_worker = lambda *_args: worker_started.set()
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")

    assert store.run_due_context_auto_retry(
        job_id, config_path, secrets_path=tmp_path / "secrets.json"
    ) is True
    assert worker_started.wait(timeout=2)
    updated = store.read(job_id)
    assert updated["context_auto_retry"]["status"] == "running"
    assert updated["context_auto_retry"]["attempts"] == 1
    assert updated["context_recovery"]["origin"] == "automatic"
    assert updated["context_recovery"]["input_fingerprint"] == "a" * 64
    store._context_recovery_active[job_id].join(timeout=2)


def test_automatic_context_retry_stops_when_saved_inputs_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id, file_id = _make_watch_job_with_audio(store, "session-changed-auto-context")
    state = store.read(job_id)
    state["status"] = "completed"
    state["context_auto_retry"] = {
        "status": "scheduled",
        "attempts": 0,
        "max_attempts": 1,
        "next_attempt_at": _iso(),
        "input_fingerprint": "b" * 64,
        "file_ids": [file_id],
    }
    store.status_path(job_id).write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(
        "life_logger.context_auto_retry.inspect_retry_candidate",
        lambda *_args: {
            "file_ids": [file_id],
            "input_fingerprint": "c" * 64,
            "failure_kinds": ["connection_error"],
        },
    )

    result = store.retry_context_extraction(
        job_id,
        tmp_path / "missing-config.json",
        automatic=True,
        expected_fingerprint="b" * 64,
    )

    assert result["context_auto_retry"]["status"] == "stopped"
    assert result["context_auto_retry"]["stopped_reason"] == "inputs_changed"
    assert job_id not in store._context_recovery_active


@pytest.mark.parametrize(
    ("resume_count", "expected_status", "expected_next_count"),
    [(0, "scheduled", 1), (1, "stopped", 1)],
)
def test_crash_recovery_resumes_same_auto_retry_at_most_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resume_count: int,
    expected_status: str,
    expected_next_count: int,
) -> None:
    root = tmp_path / "local-web"
    first = JobStore(root, recover=False)
    job_id, file_id = _make_watch_job_with_audio(first, f"session-resume-{resume_count}")
    state = first.read(job_id)
    state["status"] = "completed"
    fingerprint = "d" * 64
    state["context_auto_retry"] = {
        "status": "running",
        "attempts": 1,
        "max_attempts": 1,
        "resume_count": resume_count,
        "max_resumes": 1,
        "input_fingerprint": fingerprint,
        "file_ids": [file_id],
    }
    state["context_recovery"] = {
        "recovery_id": "1" * 32,
        "origin": "automatic",
        "input_fingerprint": fingerprint,
        "status": "running",
        "target_files": 1,
        "completed_files": 0,
        "current_stage": "context_extraction",
        "archived_artifacts": [],
    }
    first.status_path(job_id).write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(
        "life_logger.context_auto_retry.inspect_retry_candidate",
        lambda *_args: {
            "file_ids": [file_id],
            "input_fingerprint": fingerprint,
            "failure_kinds": ["http_error"],
        },
    )

    recovered = JobStore(root, recover=True)

    auto = recovered.read(job_id)["context_auto_retry"]
    assert auto["status"] == expected_status
    assert auto["resume_count"] == expected_next_count
    if expected_status == "scheduled":
        assert auto["next_attempt_at"]
    else:
        assert "next_attempt_at" not in auto
