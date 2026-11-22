"""手表上传/直连判定（S4 从 local_web.py 原样搬出）。

异常类只有一份：raise 与 except 都从这里取。
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from life_logger.web_preferences import _normalize_trusted_network_bind

def _parse_sleep_header(raw: str | None) -> dict[str, Any] | None:
    """手表上传的昨晚睡眠；格式不对就忽略，绝不影响录音分段入库。"""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        total = int(data["total_minutes"])
        datetime.strptime(str(data["date"]), "%Y-%m-%d")
        if not 0 < total <= 1440:
            return None
        return {
            "date": str(data["date"]),
            "start_time": str(data.get("start_time") or ""),
            "end_time": str(data.get("end_time") or ""),
            "total_minutes": total,
            "deep_minutes": max(0, int(data.get("deep_minutes") or 0)),
            "core_minutes": max(0, int(data.get("core_minutes") or 0)),
            "rem_minutes": max(0, int(data.get("rem_minutes") or 0)),
            "awake_minutes": max(0, int(data.get("awake_minutes") or 0)),
            "source": "apple-watch",
        }
    except (ValueError, TypeError, KeyError, OverflowError):
        return None

def _is_retryable_asr_slot_error(error: Any) -> bool:
    error_text = str(error or "")
    return (
        error_text == "ASR resource slot unavailable; job remains retryable"
        or error_text.startswith("ASR resource slot unavailable:")
    )

def _watch_direct_access_allowed(host: str, lan_access_enabled: bool) -> bool:
    if not lan_access_enabled or host in {"127.0.0.1", "localhost"}:
        return False
    if host == "0.0.0.0":
        return True
    try:
        return _normalize_trusted_network_bind(host) == host
    except ValueError:
        return False

def _watch_upload_key(root: Path) -> str:
    """The value the Watch app sends as `X-LifeLogger-Watch-Key`.

    Stored beside the access token so a deployment can rotate it without editing source; the
    fallback is the constant compiled into the Watch build.
    """
    fallback = "lifelogger-watch-upload-key-v1"
    secrets_path = Path(root) / "secrets.json"
    try:
        payload = json.loads(secrets_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    stored = str(payload.get("watch_upload_key") or "").strip()
    return stored or fallback

class _WatchUploadIncomplete(ValueError):
    """请求体短于 Content-Length（对端提前关闭）。结构化回 400。"""

    status = HTTPStatus.BAD_REQUEST

    def __init__(self, expected: int, received: int, sequence: int) -> None:
        super().__init__("segment_incomplete")
        self.payload = {
            "error": "segment_incomplete",
            "expected": int(expected),
            "received": int(received),
            "sequence": int(sequence),
            # 让客户端/日志一眼看出：不是"服务端拒收"，而是body没传完
            "hint": "body shorter than Content-Length; connection closed mid-upload",
        }

class _WatchUploadTimeout(TimeoutError):
    """上传体读取超时（对端卡住/半死连接）。结构化回 408。"""

    status = HTTPStatus.REQUEST_TIMEOUT

    def __init__(self, expected: int, received: int, sequence: int, timeout: float) -> None:
        super().__init__("segment_upload_timeout")
        self.payload = {
            "error": "segment_upload_timeout",
            "expected": int(expected),
            "received": int(received),
            "sequence": int(sequence),
            "timeout_seconds": round(float(timeout), 1),
            "hint": "no bytes received within timeout; retry this segment",
        }

class WatchUploadHelpersMixin:
    """`_Handler` 的手表直连/健康探针判定（原样搬出）。"""

    def _is_watch_health_probe(self) -> bool:
        """The connectivity probe the Watch runs before a sync.

        Kept credential-free on purpose: a Watch running a build from before the Watch
        credential existed must still be able to answer "is the Mac there", otherwise the app
        reports the Mac as unreachable instead of reporting a real refusal.
        """
        parts = urlparse(self.path).path.strip("/").split("/")
        return parts == ["api", "watch-health"] and self.command == "GET"

    def _is_watch_vip_command(self) -> bool:
        """The VIP fast-track command endpoint that Apple Watch reaches without a typed-in token."""
        parts = urlparse(self.path).path.strip("/").split("/")
        return parts == ["api", "watch", "vip-command"] and self.command == "POST"

    def _is_watch_direct_request(self) -> bool:
        """Routes a recording Watch must reach without a typed-in token.

        Deliberately narrow. This used to exempt *any* private-network caller from the bearer
        check and covered reads as well, so anything on the LAN could create sessions, read the
        user's manifests, push segments and close sessions with no credential at all. Reading a
        manifest is a read of the user's audio metadata and is no longer exempt.
        """
        parts = urlparse(self.path).path.strip("/").split("/")
        # The health probe is handled separately by `_is_watch_health_probe`, which
        # `_require_api_auth` checks before this method; keeping it here too would make the
        # exemption look broader than it is.
        if parts == ["api", "watch-status"] or parts == ["api", "watch", "vip-command"]:
            return self.command == "POST"
        if parts[:2] != ["api", "watch-sessions"]:
            return False
        # POST /api/watch-sessions — register a session.
        if parts == ["api", "watch-sessions"]:
            return self.command == "POST"
        # POST …/<session>/complete — close it.
        if len(parts) == 5 and parts[4] == "complete":
            return self.command == "POST"
        # GET …/<session>/received-segments — 断点续传查询（只读，仅序号与计数）。
        # 之所以给手表读：响应不含音频/文本/位置，泄露面只是「这台 Mac 有个会话、收到几个段」；
        # 仍要求手表内置密钥或访问令牌，不放开给任意局域网 IP。
        if len(parts) == 5 and parts[4] == "received-segments":
            return self.command == "GET"
        # PUT …/<session>/segments/<n> — push one segment.
        if len(parts) == 6 and parts[4] == "segments":
            return self.command == "PUT"
        # Everything else on this prefix (GET of a manifest, listing, deletion) keeps the
        # normal bearer-token requirement.
        return False

    def _watch_credential_matches(self) -> bool:
        expected = str(getattr(self.app, "watch_upload_key", "") or "")
        supplied = str(self.headers.get("X-LifeLogger-Watch-Key") or "")
        if not expected or not supplied:
            return False
        return secrets.compare_digest(supplied.encode("utf-8", errors="replace"), expected.encode("utf-8"))

