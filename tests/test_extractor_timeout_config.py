"""Unit tests for configurable extractor timeout in ActivityTimelineRuntime.

Tests:
1. timeout_s configured and within valid range [30, MAX] takes effect;
2. timeout_s absent/None defaults to DEFAULT_DEEPSEEK_TIMEOUT_S;
3. timeout_s invalid (below 30, above MAX, non-numeric, nan/inf) falls back to default with warning log;

[2026-11-02 A0] 断言改为引用模块常量，不再写死 60.0/600：
实测同一 prompt 正常跑完要 181s（reasoning 23,657 token），60s 根本不够，
故默认超时提到 300s、上限提到 900s。
4. Failure messages include the active timeout value (timeout=<N>s) and stay properly redacted.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from life_logger.activity_timeline_runtime import (
    DEFAULT_DEEPSEEK_TIMEOUT_S,
    MAX_DEEPSEEK_TIMEOUT_S,
    MIN_DEEPSEEK_TIMEOUT_S,
    ActivityTimelineRuntime,
)


def _make_runtime(tmp_path: Path, config_data: dict[str, Any] | None = None) -> ActivityTimelineRuntime:
    """Helper to instantiate ActivityTimelineRuntime with an isolated temp config."""
    output_root = tmp_path / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "local-config.json"
    if config_data is not None:
        config_path.write_text(json.dumps(config_data), encoding="utf-8")
    return ActivityTimelineRuntime(output_root=output_root, config_path=config_path)


class TestExtractorTimeoutConfig:
    """Tests for deepseek.timeout_s parsing and validation."""

    def test_timeout_s_configured_valid(self, tmp_path: Path):
        """1) timeout_s takes effect when configured with valid values."""
        # Test standard custom value
        rt = _make_runtime(tmp_path, {"deepseek": {"timeout_s": 120}})
        assert rt.get_deepseek_timeout_s() == 120.0

        # Test lower boundary 30
        rt_min = _make_runtime(tmp_path, {"deepseek": {"timeout_s": 30}})
        assert rt_min.get_deepseek_timeout_s() == MIN_DEEPSEEK_TIMEOUT_S

        # Test upper boundary（用常量，避免硬编码：2026-11-02 上限已由 600 提到 900）
        rt_max = _make_runtime(tmp_path, {"deepseek": {"timeout_s": MAX_DEEPSEEK_TIMEOUT_S}})
        assert rt_max.get_deepseek_timeout_s() == MAX_DEEPSEEK_TIMEOUT_S

        # Test float value
        rt_float = _make_runtime(tmp_path, {"deepseek": {"timeout_s": 45.5}})
        assert rt_float.get_deepseek_timeout_s() == 45.5

        # Test numeric string conversion
        rt_str = _make_runtime(tmp_path, {"deepseek": {"timeout_s": "90"}})
        assert rt_str.get_deepseek_timeout_s() == 90.0

    def test_timeout_s_default_when_missing(self, tmp_path: Path):
        """2) timeout_s defaults to DEFAULT_DEEPSEEK_TIMEOUT_S when missing/None/block missing."""
        # Config file missing
        rt_no_file = ActivityTimelineRuntime(
            output_root=tmp_path / "out",
            config_path=tmp_path / "non_existent_config.json",
        )
        assert rt_no_file.get_deepseek_timeout_s() == DEFAULT_DEEPSEEK_TIMEOUT_S

        # deepseek block empty
        rt_empty_ds = _make_runtime(tmp_path, {"deepseek": {}})
        assert rt_empty_ds.get_deepseek_timeout_s() == DEFAULT_DEEPSEEK_TIMEOUT_S

        # deepseek.timeout_s is None
        rt_none = _make_runtime(tmp_path, {"deepseek": {"timeout_s": None}})
        assert rt_none.get_deepseek_timeout_s() == DEFAULT_DEEPSEEK_TIMEOUT_S

        # deepseek key not in config
        rt_no_ds = _make_runtime(tmp_path, {"activity_timeline": {"enabled": True}})
        assert rt_no_ds.get_deepseek_timeout_s() == DEFAULT_DEEPSEEK_TIMEOUT_S

    @pytest.mark.parametrize(
        "invalid_val",
        [
            10,            # below 30
            0,             # zero
            -15,           # negative
            29.9,          # slightly below 30
            MAX_DEEPSEEK_TIMEOUT_S + 0.1,   # 略超上限
            1000,          # far above 600
            "invalid_str", # non-numeric string
            [],            # list
            {},            # dict
            float("nan"),  # NaN
            float("inf"),  # Infinity
        ],
    )
    def test_timeout_s_invalid_fallback_and_logged(self, tmp_path: Path, invalid_val, caplog):
        """3) timeout_s falls back to default and logs warning when given invalid values."""
        rt = _make_runtime(tmp_path, {"deepseek": {"timeout_s": invalid_val}})
        with caplog.at_level(logging.WARNING):
            val = rt.get_deepseek_timeout_s()
        assert val == DEFAULT_DEEPSEEK_TIMEOUT_S
        assert any("Invalid deepseek.timeout_s" in record.message for record in caplog.records)


class TestCallDeepseekOnceTimeoutBehavior:
    """Tests for _call_deepseek_once respecting timeout and reporting it on failures."""

    def test_call_passes_configured_timeout_to_urlopen(self, tmp_path: Path):
        """_call_deepseek_once passes the configured timeout_s to urllib.request.urlopen."""
        rt = _make_runtime(tmp_path, {"deepseek": {"timeout_s": 150.0}})
        tier = {
            "name": "primary",
            "base_url": "http://127.0.0.1:3425/v1",
            "api_key": "test_key",
            "model": "deepseek-v4.1-flash",
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": json.dumps({"activities": []})}}]
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            res = rt._call_deepseek_once(tier, "test prompt")
            assert res == {"activities": []}
            mock_urlopen.assert_called_once()
            _, kwargs = mock_urlopen.call_args
            assert kwargs.get("timeout") == 150.0

    def test_call_default_timeout_passed_to_urlopen(self, tmp_path: Path):
        """_call_deepseek_once passes the configured default timeout to urlopen when unconfigured."""
        rt = _make_runtime(tmp_path, {"deepseek": {}})
        tier = {
            "name": "primary",
            "base_url": "http://127.0.0.1:3425/v1",
            "api_key": "test_key",
            "model": "deepseek-v4.1-flash",
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": json.dumps({"ok": True})}}]
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            rt._call_deepseek_once(tier, "test prompt")
            mock_urlopen.assert_called_once()
            _, kwargs = mock_urlopen.call_args
            assert kwargs.get("timeout") == DEFAULT_DEEPSEEK_TIMEOUT_S

    def test_timeout_value_appears_in_timeout_error_message(self, tmp_path: Path):
        """TimeoutError message includes (timeout=<timeout_s>s)."""
        rt = _make_runtime(tmp_path, {"deepseek": {"timeout_s": 180}})
        tier = {
            "name": "primary",
            "base_url": "http://127.0.0.1:3425/v1",
            "api_key": "secret_key_12345",
            "model": "deepseek-v4.1-flash",
        }

        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with pytest.raises(RuntimeError) as exc_info:
                rt._call_deepseek_once(tier, "test prompt")
            msg = str(exc_info.value)
            assert "timeout=180s" in msg
            assert "TimeoutError" in msg
            # Check credentials are redacted
            assert "secret_key_12345" not in msg

    def test_timeout_value_appears_in_urlerror_message(self, tmp_path: Path):
        """URLError message includes (timeout=<timeout_s>s)."""
        rt = _make_runtime(tmp_path, {"deepseek": {"timeout_s": 90}})
        tier = {
            "name": "primary",
            "base_url": "http://127.0.0.1:3425/v1",
            "api_key": "secret_key_12345",
            "model": "deepseek-v4.1-flash",
        }

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            with pytest.raises(RuntimeError) as exc_info:
                rt._call_deepseek_once(tier, "test prompt")
            msg = str(exc_info.value)
            assert "timeout=90s" in msg
            assert "URLError" in msg
            assert "secret_key_12345" not in msg

    def test_timeout_value_appears_in_httperror_message(self, tmp_path: Path):
        """HTTPError message includes (timeout=<timeout_s>s)."""
        rt = _make_runtime(tmp_path, {"deepseek": {"timeout_s": 45}})
        tier = {
            "name": "primary",
            "base_url": "http://127.0.0.1:3425/v1",
            "api_key": "secret_key_12345",
            "model": "deepseek-v4.1-flash",
        }

        http_err = urllib.error.HTTPError(
            url="http://127.0.0.1:3425/v1/chat/completions",
            code=504,
            msg="Gateway Timeout",
            hdrs={},
            fp=MagicMock(read=lambda: b'{"error": "gateway timed out"}'),
        )
        with patch("urllib.request.urlopen", side_effect=http_err):
            with pytest.raises(RuntimeError) as exc_info:
                rt._call_deepseek_once(tier, "test prompt")
            msg = str(exc_info.value)
            assert "timeout=45s" in msg
            assert "HTTP 504" in msg
            assert "secret_key_12345" not in msg

    def test_default_deepseek_extractor_aggregates_timeout_info(self, tmp_path: Path):
        """_default_deepseek_extractor reflects timeout in aggregated failure text."""
        rt = _make_runtime(tmp_path, {
            "deepseek": {
                "base_url": "http://127.0.0.1:3425/v1",
                "api_key": "magpie",
                "model": "test-primary",
                "tier2_model": "test-tier2",
                "fallback": {
                    "enabled": False,
                },
                "timeout_s": 75,
            }
        })

        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with pytest.raises(RuntimeError) as exc_info:
                rt._default_deepseek_extractor("prompt text")
            msg = str(exc_info.value)
            assert "all extraction tiers failed" in msg
            assert "timeout=75s" in msg
