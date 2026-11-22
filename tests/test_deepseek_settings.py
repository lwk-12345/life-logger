"""Local-only tests for DeepSeek settings and secret handling."""

from __future__ import annotations

import json
import stat
import threading
import urllib.error
import urllib.request
from pathlib import Path

from life_logger.local_web import JobStore, create_server

TEST_KEY = "test-only-deepseek-key-do-not-use"


def _post_json(url: str, payload: dict[str, object]):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(request)


def test_deepseek_settings_save_secret_preserve_blank_and_redact_get(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    server = create_server(
        host="127.0.0.1", port=0, store=store, config_path=config_path
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        initial = json.loads(urllib.request.urlopen(f"{base}/api/settings").read())
        assert initial["deepseek"] == {
            "enabled": False,
            "model": "deepseek-flash",
            "base_url": "https://api.deepseek.com",
            "key_configured": False,
        }
        assert TEST_KEY not in json.dumps(initial)

        saved = json.loads(
            _post_json(
                f"{base}/api/settings/deepseek",
                {"api_key": TEST_KEY, "enabled": True},
            ).read()
        )
        assert saved["saved"] is True
        assert saved["deepseek"]["enabled"] is True
        assert saved["deepseek"]["key_configured"] is True

        secret_path = store.root / "secrets.json"
        assert (
            json.loads(secret_path.read_text(encoding="utf-8"))["deepseek_api_key"]
            == TEST_KEY
        )
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600

        # An empty key field is the UI's preserve-existing-key behavior.
        preserved = json.loads(
            _post_json(
                f"{base}/api/settings/deepseek", {"api_key": "", "enabled": True}
            ).read()
        )
        assert preserved["deepseek"]["key_configured"] is True
        assert (
            json.loads(secret_path.read_text(encoding="utf-8"))["deepseek_api_key"]
            == TEST_KEY
        )

        after = json.loads(urllib.request.urlopen(f"{base}/api/settings").read())
        assert after["deepseek"]["enabled"] is True
        assert after["deepseek"]["key_configured"] is True
        assert TEST_KEY not in json.dumps(after)

        try:
            _post_json(
                f"{base}/api/settings/deepseek",
                {"api_key": "", "enabled": "false"},
            )
        except urllib.error.HTTPError as error:
            assert error.code == 400
            assert "布尔值" in error.read().decode("utf-8")
        else:
            raise AssertionError(
                "enabled string must be rejected instead of truth-coerced"
            )

        # Validation failure must leave the previous setting intact.
        after_invalid = json.loads(
            urllib.request.urlopen(f"{base}/api/settings").read()
        )
        assert after_invalid["deepseek"]["enabled"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_enabling_deepseek_without_key_is_rejected(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    server = create_server(
        host="127.0.0.1", port=0, store=store, config_path=config_path
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/settings/deepseek"
        try:
            _post_json(url, {"api_key": "", "enabled": True})
        except urllib.error.HTTPError as error:
            assert error.code == 400
            assert "API Key" in error.read().decode("utf-8")
        else:
            raise AssertionError("enabling DeepSeek without a key must fail")
        assert not (store.root / "secrets.json").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
