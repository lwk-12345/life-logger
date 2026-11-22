"""鉴权与设置读写（S3 从 local_web.py 原样搬出，鉴权逻辑逐字不变）。"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

def _read_secret_settings(root: Path) -> dict[str, Any]:
    path = Path(root) / "secrets.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}

def _write_secret_settings(root: Path, payload: dict[str, Any]) -> None:
    path = Path(root) / "secrets.json"
    if payload:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".secrets.", suffix=".tmp", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
            os.chmod(path, 0o600)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(temporary_name).unlink(missing_ok=True)
            raise
    else:
        path.unlink(missing_ok=True)

def _ensure_mobile_access_token(root: Path) -> str:
    payload = _read_secret_settings(root)
    current = str(payload.get("mobile_access_token") or "").strip()
    if current:
        return current
    token = secrets.token_urlsafe(24)
    payload["mobile_access_token"] = token
    _write_secret_settings(root, payload)
    return token

def _mobile_capture_config(preferences: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "mobile-capture-v1",
        "enabled": bool(preferences.get("mobile_capture_enabled", True)),
        "auto_start": bool(preferences.get("mobile_capture_auto_start", False)),
        "transport": "same-origin-job-upload",
        "requires_token": True,
        "public_upload": False,
        "accepted_media": [
            "wav",
            "m4a",
            "mp3",
            "flac",
            "aac",
            "ogg",
            "opus",
            "mp4",
            "mov",
            "webm",
        ],
        "job_upload_path": "/api/jobs/<job_id>/files?name=<filename>",
    }

class AuthHelpersMixin:
    """`_Handler` 的令牌/鉴权助手（原样搬出）。_send_json 由 web_support 的 mixin 提供。"""

    def _request_token(self) -> str:
        authorization = str(self.headers.get("Authorization") or "")
        if authorization.startswith("Bearer "):
            return authorization[7:].strip()
        raw_cookie = str(self.headers.get("Cookie") or "")
        if raw_cookie:
            cookie = SimpleCookie()
            try:
                cookie.load(raw_cookie)
            except Exception:
                return ""
            morsel = cookie.get("life_logger_access")
            if morsel is not None:
                return str(morsel.value or "")
        return ""

    def _token_matches(self, token: str) -> bool:
        expected = str(self.app.access_token or "")
        return bool(expected and token and secrets.compare_digest(
            token.encode("utf-8", errors="replace"), expected.encode("utf-8")
        ))

    def _require_api_auth(self, *, write: bool = False) -> bool:
        if self.app.watch_local_access_enabled and self._is_watch_health_probe():
            return True
        if self.app.watch_local_access_enabled and self._is_watch_vip_command():
            return True
        if self.app.watch_local_access_enabled and self._is_watch_direct_request():
            # The Watch cannot type a token, so these direct write routes accept the credential
            # compiled into the app instead. Note the ordering: a Watch write request carries
            # no Origin/CSRF header, so the browser-only CSRF gate below must not apply to it —
            # which is why this branch checks a credential of its own rather than trusting the
            # source address.
            #
            # A failed key must NOT short-circuit: a caller that presents a valid access token
            # (the web console, or a Watch build predating the key) falls through to the normal
            # check below. Only when neither credential is good does the request get refused.
            if self._watch_credential_matches():
                return True
            if not self._token_matches(self._request_token()):
                self._send_json(
                    {
                        "error": "unauthorized",
                        "detail": "Watch 专用接口需要手表密钥（X-LifeLogger-Watch-Key）或访问令牌",
                    },
                    HTTPStatus.UNAUTHORIZED,
                )
                return False
            return True
        if not self.app.access_token:
            return True
        if not self._token_matches(self._request_token()):
            self._send_json(
                {"error": "unauthorized", "detail": "Life Logger API 需要访问令牌"},
                HTTPStatus.UNAUTHORIZED,
            )
            return False
        if not write:
            return True
        origin = str(self.headers.get("Origin") or "")
        host = str(self.headers.get("Host") or "")
        csrf = str(self.headers.get("X-Life-Logger-CSRF") or "")
        try:
            origin_parts = urlparse(origin)
            same_origin = (
                origin_parts.scheme.lower() in {"http", "https"}
                and origin_parts.netloc.casefold() == host.casefold()
                and not origin_parts.path
                and not origin_parts.params
                and not origin_parts.query
                and not origin_parts.fragment
                and origin_parts.username is None
                and origin_parts.password is None
            )
        except ValueError:
            same_origin = False
        if csrf != "1" or not host or not same_origin:
            self._send_json(
                {"error": "forbidden", "detail": "写操作需要同源请求与 CSRF 头"},
                HTTPStatus.FORBIDDEN,
            )
            return False
        return True

